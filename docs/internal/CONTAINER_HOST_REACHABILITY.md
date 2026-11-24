# Container → host reachability on Jupiter (2026-11-21)

**Status (2026-11-22):** option 2 is **installed on Jupiter in observe mode**: it logs and blocks nothing.
- **Enforce waits for three things,** listed in `monitoring/README.md` → Container → host firewall → Observe:
  - monitoring back: done 2026-11-24, after the data disks moved into Jupiter;
  - the compose change below deployed;
  - a day of observe logs.
- **Files:** `scripts/container-firewall/` (script, config, unit, probe).
- **Operations runbook:** `monitoring/README.md`. Install record: [below](#installed-2026-11-22).

Follow-up to [`CODER_SPIKE.md`](CODER_SPIKE.md) §1 (PR #174), which found that a plain `internal: true` network reaches Jupiter's host services through its bridge address. This note asks the same about every Docker network on Jupiter.

## Summary

- **Every container on an ordinary Docker network can reach every Jupiter host service that listens on all interfaces.** That is 17 of the 18 Docker networks, i.e. every running container except Home Assistant and Pi-hole (those two are host-networked).
  - This includes SSH, SMB, Pi-hole (recursive DNS + admin), Home Assistant, Netdata, node-exporter, Conjure and go2rtc.
  - It also includes VoxCPM TTS, which was bound to `docker0` to keep it off the LAN. Every container reaches it.
  - It works through **any** host address: the container's own bridge gateway, `docker0`, the LAN IP, even the tailnet IP.
- **Binding a service to the LAN interface does not keep containers out.** Containers reach the LAN IP too. Only a `127.0.0.1` or `tailscale0` bind does, and those binds are already refused (measured).
- **Network design alone can't fix this for the containers that matter.**
  - `gateway_mode_ipv4: isolated` is refused on a non-internal network. Docker 29.1.3 says: *"gateway mode 'isolated' can only be used for an internal network"*.
  - Every container that handles outside content sits on at least one NAT network, because it needs the internet: searxng, Immich, Audiobookshelf, Paperless, Grist and Homepage.
- **IPv6 too.** A container with `NET_RAW` (in Docker's default capability set, so root containers have it) reaches the same services through the bridges' IPv6 link-local addresses, by sending hand-made packets. Its own IPv6 being disabled doesn't stop that.
- **Recommended fix:** a small host `INPUT` chain per IP family for traffic that arrives *from Docker bridges only*. It allows the few (network, port) pairs that are really needed and rejects the rest. LAN, tailnet and loopback traffic never enters the chains, so they cannot lock out SSH.

## Method

A throwaway probe container ran on each of the 18 bridge networks:
- orchestrator image, `--cap-drop ALL`, `no-new-privileges`, `--rm`;
- a stdlib Python TCP-connect script (1.5 s timeout), plus a real DNS query (`example.com A`) on UDP 53.

The probe is `scripts/container-firewall/probe_host_ports.py`; its docstring has the loop that runs it on every network.

**Targets:**
- **addresses:** the network's own gateway, the LAN IP `10.0.0.248`, `docker0` `172.17.0.1` and the tailnet IP;
- **ports:** every TCP port from `ss -ltnp`, plus the Tailscale Serve and loopback-only ones, to confirm they stay closed.

IPv6: no network has IPv6 enabled, and containers have IPv6 disabled on `eth0`. So the probe is IPv4 only. But the host's bridges have link-local IPv6 addresses, and the services listen on `[::]`.
- **Test (security review, 2026-11-22):** a root container with only `NET_RAW` sent raw IPv6 SYNs to `docker0`'s link-local address.
- **Result:** SSH, SMB, HA and Netdata answered.
- **Coverage:** `JESS-DOCKER-IN6` now counts and logs exactly these packets. Enforce mode drops them.

## Findings: host services

"NAT networks" means all 17 non-internal networks. Every one of them gave the same result.

| Host service | Port | Bound to | From NAT networks | From `searxng-probe-net` (internal) | Needed by a container? |
|---|---|---|---|---|---|
| SSH | 22 | all | **open** | **open** | no |
| Pi-hole DNS (recursive, forwards to the internet) | 53 tcp+udp | all | **open, answers** | **open, answers** | no: containers use Docker's resolver, which forwards to the router `10.0.0.1` |
| SMB | 139, 445 | all | **open** | **open** | no |
| Conjure API | 5050 | all | **open** | **open** | Prometheus scrape + blackbox `/health` |
| VoxCPM TTS | 8004 | `172.17.0.1` only | **open** (via `172.17.0.1`) | no | orchestrator, via `host.docker.internal`, once TTS moves to Jupiter |
| Pi-hole admin/API | 8053 | all | **open** | **open** | orchestrator (`PIHOLE_URLS`) |
| Home Assistant | 8123 | all (host network) | **open** | **open** | orchestrator (`HA_URL=http://10.0.0.248:8123`) |
| node-exporter | 9100 | all | **open** | **open** | Prometheus |
| NVIDIA GPU exporter | 9400 | LAN IP only | **open** (via LAN IP) | no | Prometheus, once a Jupiter GPU job exists |
| go2rtc (inside HA) | 18555 | all | **open** | **open** | no |
| Netdata | 19999 | all | **open** | **open** | Prometheus |
| Published `0.0.0.0` ports: orchestrator 8888, Immich 2283, Audiobookshelf 13378 | — | docker-proxy | open | timeout | Prometheus, blackbox and Alertmanager → orchestrator. These go through Docker's DNAT + `FORWARD` path, not `INPUT` (see below) |
| Loopback publishes (3001, 5006, 5232, 7575, 8090, 8484, 9115), Tailscale Serve ports, Netdata statsd 8125 | — | `127.0.0.1` / `tailscale0` | refused | refused | — |

**Not probed** (UDP without a protocol to test): nmbd 137/138, HA mDNS 5353 and SSDP 1900 also listen on every bridge address. The proposed rule covers them too.

**Also reachable from every NAT network:** the internet and the LAN (e.g. Helios `:8080`). That is the `FORWARD` path, out of scope here; see [`CONTAINER_LAN_REACHABILITY.md`](CONTAINER_LAN_REACHABILITY.md).

## Findings: networks

| Network | Kind | Members (2026-11-21) |
|---|---|---|
| `gateway_nerves_gateway-net` | NAT | orchestrator, frontend, radicale, actual-budget, redis, loki |
| `monitoring_monitoring-net` | NAT | prometheus, grafana, alertmanager, blackbox, promtail, radicale, actual-budget, loki |
| `gateway_nerves_searxng-net` | NAT | searxng, orchestrator |
| `gateway_nerves_grist-net` | NAT | grist, orchestrator |
| `gateway_nerves_searxng-probe-net` | **internal** | searxng, blackbox |
| `gateway_nerves_frontend-probe-net` | NAT | frontend, blackbox |
| `monitoring_grist-probe-net` | NAT | grist, blackbox |
| `monitoring_loki-push-net` | NAT | loki-push-gw, loki |
| `immich_default` | NAT | immich_server, immich_postgres, immich_redis |
| `audiobookshelf_default` | NAT | audiobookshelf |
| `homepage_default` | NAT | homepage |
| `paperless_default` | NAT | Paperless (stopped at measurement time) |
| `bridge` (`docker0`) | NAT | ad-hoc `docker run` only |
| `actual_default`, `radicale_default`, `gateway_nerves_default`, `gateway_mvp_default`, `gateway_mvp_gateway-net` | NAT | none (left over) |
| `gateway_nerves_host-access` (new, PR #176) | NAT | orchestrator only (its default route) |
| `monitoring_host-scrape` (new, PR #176) | NAT | prometheus, blackbox-exporter (their default route) |

## What containers need from the host

Only these use the `INPUT` path, i.e. connect to a process on Jupiter itself:

| From | To | Why |
|---|---|---|
| orchestrator | 8123 | `HA_URL` is the LAN IP. `ip route get 10.0.0.248 from 172.26.0.2 iif br-…` gives `local`, so the packet arrives on the bridge the orchestrator left through. |
| orchestrator | 8053 | Pi-hole client (`PIHOLE_URLS`) |
| orchestrator | 8004 | VoxCPM via `host.docker.internal` (= `172.17.0.1`), once `TTS_URL` points at Jupiter |
| orchestrator | 8888 | A call to `10.0.0.248:8888` from the orchestrator's publishing network is not DNAT'd. It hairpins through docker-proxy on `INPUT`. |
| Prometheus, blackbox | 5050, 8004, 9100, 9400, 19999 | scrape jobs `node-exporter`, `conjure`, `netdata` and the Jupiter GPU exporter; blackbox `conjure` probe and `tts-voxcpm` probe (`http://host.docker.internal:8004/health`, since VoxCPM moved to Jupiter in PR #172) |

**Which bridge a packet arrives on.** It is the bridge of the container's **default-route** network, not of "its" network.
- With no `gw_priority`, Docker picks the non-internal network whose name sorts first.
- Live on 2026-11-22, blackbox-exporter left through `frontend-probe-net`, not monitoring-net. The orchestrator left through gateway-net only because that name sorts before grist-net and searxng-net.
- That is why the allow lines name two dedicated networks, each the `gw_priority` default route of its only members (below).

Everything else goes container-to-container by name, or reaches a **published** port. From another network, Docker DNATs that connection in `PREROUTING` and sends it through `FORWARD`, so `INPUT` rules don't touch it. Examples: Prometheus, blackbox and Alertmanager → `10.0.0.248:8888`, and blackbox → Paperless `:8777`.

Host-side jobs are also outside the chain: backups, CI runner, Tailscale Serve, cron. They are host processes (the `OUTPUT` path).

## Options

### 1. `isolated` gateway mode on the internal probe network

- **Change:** one `driver_opts` line on `searxng-probe-net` in `docker-compose.yml`. It needs the network recreated.
  - blackbox → searxng is container-to-container, which the spike showed still works under `isolated`.
- **Closes in practice:** nothing.
  - searxng is also on `searxng-net` (NAT), and blackbox on `monitoring-net` (NAT). Both still reach every host service through those.
  - `frontend-probe-net` and `grist-probe-net` are the same story. They could become `internal` + `isolated`, but their members also sit on NAT networks.
- **Verdict:** hygiene only. It makes "internal" mean what it says. Optional, and independent of option 2.

### 2. Host `INPUT` allow-list for Docker bridges (recommended)

- **Closes:** everything not in the table above, for all 18 networks, including UDP and (with the IPv6 chain) raw IPv6.
- **Can't lock out SSH:** the chain only sees packets that arrive on `br-*` or `docker0`. SSH from the LAN (`enp7s0`) or tailnet (`tailscale0`) never enters it.
- **Cost:** a short script plus a systemd unit installed on Jupiter by hand, like `scripts/usb-storage/`.

### 3. Rebind host services

- **Change:** move a service's listener off the shared addresses.
- **Works only** for a `127.0.0.1` or `tailscale0` bind. Containers reach the LAN IP, so a LAN-interface bind still lets them in.
- **Candidates:** Pi-hole (idle since late Sept 2026; it could go loopback-only or stop) and go2rtc's WebRTC port.
- **Can't use it for:** SSH, SMB, HA, Netdata or node-exporter. They must stay on the LAN, or are scraped from a container.
- **Verdict:** not a fix. At most a small extra once option 2 is in.

## Recommended change (option 2) — what is installed

### Rules

`sudo jess-docker-input render` prints the exact rules for the current config and networks.

- **Chains:** `JESS-DOCKER-IN` (iptables) and `JESS-DOCKER-IN6` (ip6tables), each jumped from INPUT positions 1–2 for `-i br-+` and `-i docker0` only.
- **IPv4 order:**
  1. Replies to connections the **host** opened (`--ctdir REPLY`) pass. The host calling a container (`OUTPUT`) gets its answers back through `INPUT` from the bridge.
  2. Conntrack `INVALID` and non-unicast packets: dropped silently in enforce, passed in observe.
  3. The `allow` lines: `-i <bridge> -p tcp -m multiport --dports … -j RETURN`.
     - They are checked on **every** packet of a container-opened connection, not only the first.
     - So a connection opened during observe mode, or to a port later removed from the list, is cut once enforce is on.
  4. A counting rule, then a `LOG` rule:
     - rate-limited per flow (2/min per source, destination and port) and 30/min overall;
     - kernel header fields only: addresses, ports, MACs.
  5. Enforce mode only: `REJECT --reject-with tcp-reset` for TCP, `DROP` for the rest.
- **IPv6:** host replies pass; everything else is counted, logged and (enforce) dropped. There are no allow lines, because no container uses IPv6.
- **Return, not accept:** allowed packets `RETURN` to INPUT, which still runs `ts-input` and has policy `ACCEPT`.

### The allowed networks

| Network | Members | Fixed bridge | Allowed host ports |
|---|---|---|---|
| `gateway_nerves_host-access` | orchestrator (`gw_priority: 1`) | `br-jess-hostacc` | 8004, 8053, 8123, 8888 |
| `monitoring_host-scrape` | prometheus, blackbox-exporter (`gw_priority: 1`) | `br-jess-scrape` | 5050, 8004, 9100, 9400, 19999 |

- **Why dedicated networks:**
  - Only the containers that need host access get it. On gateway-net, radicale, actual-budget, loki and the frontend would have got HA too.
  - The orchestrator's plain-HTTP HA traffic no longer shares a bridge (L2) with other containers.
- **A fixed bridge name** keeps the rule valid if the network is ever recreated.
- **Until the compose change is deployed,** the config also carries two `TRANSITION` lines for gateway-net and monitoring-net. Delete them before enforce.

### How it is applied

- **Watcher:** `jess-docker-input watch` (unit `jess-docker-input.service`) resolves the bridge names from the network names at apply time.
  - It refills each chain with one `*-restore --noflush` transaction.
  - It re-checks every 60 s and on every Docker network `create`/`destroy`.
- **Drift is repaired without touching a healthy chain:** a chain whose digest or rule count is wrong, a deleted jump, a jump pushed down by a tailscaled restart, a recreated network.
- **Failure handling:**
  - A bad config or an unreachable dockerd leaves the chains as they are.
  - `systemctl reload` validates first and fails loudly on a bad config.
  - A clean stop removes the chains (that is the rollback); a crash keeps them for the restart.
- **Survives reboots** without iptables-persistent: the unit rebuilds the chains at boot.
- **Nothing else rewrites them:** Docker doesn't touch `INPUT`, and Tailscale only manages `ts-input`.
- **Small boot window:** the chains are in place a few seconds after `docker.service` starts.

### Rollout, rollback, alerts

See `monitoring/README.md` → Container → host firewall. In short:
1. **Observe** (now).
2. **Gates:** monitoring healthy 24 h; compose change deployed and `TRANSITION` lines gone; 24 h of observe covering backups, the self-audit, a CI deploy and Paperless; `summary` clean; alerts loaded.
3. **Enforce:** `mode enforce` + reload, then re-probe with new connections.

Rollback is `sudo systemctl disable --now jess-docker-input`.

## Installed (2026-11-22)

- **First version (observe):** installed 04:13 UTC. The reviews found:
  - the IPv6 gap;
  - the default-route bridge issue;
  - gaps in drift, metrics and failure handling.
- **Current version:** reinstalled 04:34–04:37.
- **Checked on Jupiter:**
  - **Other rules untouched:** every rule outside the two chains is identical to the pre-install `iptables-save`/`ip6tables-save`, Docker's included.
  - **Observe blocks nothing:** probe containers on `immich_default`, `gateway_nerves_gateway-net` and `searxng-probe-net` reach the same ports as before.
  - **Logging works:** the unmatched packets are logged. `summary` lists them by network, address and port.
  - **IPv6 is now visible:** the raw IPv6 SYNs from a root `NET_RAW` container are counted (0 → 9) and logged by `JESS-DOCKER-IN6`. They still get through in observe, as designed.
  - **Allowed paths work:** orchestrator → HA `/api/` returns 200, and → VoxCPM via `host.docker.internal` returns 200, both through the allow rule.
  - **Drift repair works:** a foreign rule inserted at INPUT 1 and a deleted IPv6 jump were put back within 60 s. The jump-only repair leaves the chain's counters alone.
  - **Bad config:** `systemctl reload` failed and the rules stayed. A good reload then succeeded.
  - **Crash:** after `kill -9`, the unit kept the chains ("keeping the chains for the restart") and re-applied them.
  - **Metrics:** the textfile metrics appear on node-exporter `:9100/metrics`.
- **Still to check, once host-scrape exists:** Prometheus scrapes and the blackbox probes through host-scrape, and the Alertmanager webhook.

**2026-11-24 (after the reboot at 00:35 UTC):**
- **Reboot survived:** the unit re-applied both chains, jumps on top, observe mode.
- **Monitoring back:** all Jupiter scrapes (`node-exporter`, `netdata`, `conjure`) and `probe_success{service="conjure"}` = 1. The `jess_docker_input_*` metrics reach Prometheus.
- **The first 16 minutes of observe logs confirm the default-route problem in real traffic.**
  - blackbox-exporter's Conjure probe (`10.0.0.248:5050`) and its new TTS probe (`host.docker.internal:8004`, since VoxCPM moved to Jupiter) both left through `frontend-probe-net`. In enforce mode both would have been rejected.
  - Fix: `host-scrape` carries blackbox's default route, and its allow line gained 8004.
- **Ignore the self-port hits** (`immich_default` → :2283, `audiobookshelf_default` → :13378, `paperless_default` → :8777, `monitoring_loki-push-net` → :3101). They came from throwaway probe containers another session started at 00:49–00:51, not from the apps.

## Not covered by this change

- **The allow-list is per network, not per container.** That is why the two allowed networks hold only the containers that need them. Adding a container to host-access or host-scrape gives it those ports.
- **Published `0.0.0.0` ports** (8888, 2283, 13378) stay reachable from every container, through DNAT and `FORWARD`. They are LAN-facing by design anyway.
- **Container → LAN and container → internet** (`FORWARD`) are untouched. Any container can still reach Helios, the router and IoT devices. Measured 2026-11-24, with a proposed `FORWARD` allow-list: [`CONTAINER_LAN_REACHABILITY.md`](CONTAINER_LAN_REACHABILITY.md).
- **Allow lines can be borrowed.** A root container with `NET_RAW` that shares a network with the orchestrator, Prometheus or blackbox can route packets through it, because containers inherit `ip_forward = 1`. The packets then arrive on `host-access`/`host-scrape` and match their allow lines. A source-address check per bridge closes it; it is proposed in [`CONTAINER_LAN_REACHABILITY.md`](CONTAINER_LAN_REACHABILITY.md) → Chains.
- **The LAN and the tailnet still reach these host services**, since ufw is inactive. This change is about containers only.
- **promtail and Homepage mount the Docker socket.** A `:ro` bind does not stop API calls (tested: `GET /version` → 200 through a read-only mount), so both are root-equivalent on Jupiter. No firewall rule contains them.
- **`coder-net`** (see `CODER_PLAN.md`) is `isolated`, so it has no host address. It still needs its fixed bridge name to start with `br-`, so the chains cover it as the backstop the spike asks for. A bridge outside `br-*`/`docker0` shows up as `jess_docker_input_networks_uncovered` → `ContainerFirewallCheckFailing`.
- **`NET_RAW` stays in Docker's default capabilities.** brain-frontend and blackbox-exporter (http probes only) could drop it. The IPv6 chain covers the host side either way.
