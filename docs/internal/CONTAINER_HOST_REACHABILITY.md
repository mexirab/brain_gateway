# Container → host reachability on Jupiter (2026-11-21)

**Status:** measured; change **proposed, not applied**. Applying it is a manual host step that needs the owner's OK.

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
- **Recommended fix:** a small host `INPUT` chain for traffic that arrives *from Docker bridges only*. It allows the few (network, port) pairs that are really needed and rejects the rest. LAN, tailnet and loopback traffic never enters the chain, so it cannot lock out SSH.

## Method

A throwaway probe container ran on each of the 18 bridge networks:
- orchestrator image, `--cap-drop ALL`, `no-new-privileges`, `--rm`;
- a stdlib Python TCP-connect script (1.5 s timeout), plus a real DNS query (`example.com A`) on UDP 53.

**Targets:**
- **addresses:** the network's own gateway, the LAN IP `10.0.0.248`, `docker0` `172.17.0.1` and the tailnet IP;
- **ports:** every TCP port from `ss -ltnp`, plus the Tailscale Serve and loopback-only ones, to confirm they stay closed.

IPv6 needs no test: no network has IPv6 enabled, and containers have IPv6 disabled on `eth0`.

## Findings: host services

"NAT networks" means all 17 non-internal networks. Every one of them gave the same result.

| Host service | Port | Bound to | From NAT networks | From `searxng-probe-net` (internal) | Needed by a container? |
|---|---|---|---|---|---|
| SSH | 22 | all | **open** | **open** | no |
| Pi-hole DNS (recursive, forwards to the internet) | 53 tcp+udp | all | **open, answers** | **open, answers** | no: containers use Docker's resolver, which forwards to the router `10.0.0.1` |
| SMB | 139, 445 | all | **open** | **open** | no |
| Conjure API | 5050 | all | **open** | **open** | Prometheus scrape + blackbox `/health` (monitoring-net) |
| VoxCPM TTS | 8004 | `172.17.0.1` only | **open** (via `172.17.0.1`) | no | orchestrator, via `host.docker.internal`, once TTS moves to Jupiter |
| Pi-hole admin/API | 8053 | all | **open** | **open** | orchestrator (`PIHOLE_URLS`) |
| Home Assistant | 8123 | all (host network) | **open** | **open** | orchestrator (`HA_URL=http://10.0.0.248:8123`) |
| node-exporter | 9100 | all | **open** | **open** | Prometheus (monitoring-net) |
| NVIDIA GPU exporter | 9400 | LAN IP only | **open** (via LAN IP) | no | Prometheus, once a Jupiter GPU job exists |
| go2rtc (inside HA) | 18555 | all | **open** | **open** | no |
| Netdata | 19999 | all | **open** | **open** | Prometheus (monitoring-net) |
| Published `0.0.0.0` ports: orchestrator 8888, Immich 2283, Audiobookshelf 13378 | — | docker-proxy | open | timeout | Prometheus, blackbox and Alertmanager → orchestrator. These go through Docker's DNAT + `FORWARD` path, not `INPUT` (see below) |
| Loopback publishes (3001, 5006, 5232, 7575, 8090, 8484, 9115), Tailscale Serve ports, Netdata statsd 8125 | — | `127.0.0.1` / `tailscale0` | refused | refused | — |

**Not probed** (UDP without a protocol to test): nmbd 137/138, HA mDNS 5353 and SSDP 1900 also listen on every bridge address. The proposed rule covers them too.

**Also reachable from every NAT network:** the internet and the LAN (e.g. Helios `:8080`). That is the `FORWARD` path, by design, and out of scope here.

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

## What containers need from the host

Only these use the `INPUT` path, i.e. connect to a process on Jupiter itself:

| From | To | Why |
|---|---|---|
| orchestrator (gateway-net) | 8123 | `HA_URL` is the LAN IP. `ip route get 10.0.0.248 from 172.26.0.2 iif br-…` gives `local`, so the packet arrives on the gateway-net bridge. |
| orchestrator (gateway-net) | 8053 | Pi-hole client (`PIHOLE_URLS`) |
| orchestrator (gateway-net) | 8004 | VoxCPM via `host.docker.internal` (= `172.17.0.1`), once `TTS_URL` points at Jupiter |
| gateway-net members | 8888 | A call to `10.0.0.248:8888` from the orchestrator's own network is not DNAT'd. It hairpins through docker-proxy on `INPUT`. |
| Prometheus, blackbox (monitoring-net) | 5050, 9100, 19999, (9400) | scrape jobs `node-exporter`, `conjure` and `netdata`, plus the blackbox `conjure` probe; 9400 for a future Jupiter GPU job |

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

- **Closes:** everything not in the table above, for all 18 networks, including UDP.
- **Can't lock out SSH:** the chain only sees packets that arrive on `br-*` or `docker0`. SSH from the LAN (`enp7s0`) or tailnet (`tailscale0`) never enters it.
- **Cost:** a short script plus a systemd unit installed on Jupiter by hand, like `scripts/usb-storage/`.

### 3. Rebind host services

- **Change:** move a service's listener off the shared addresses.
- **Works only** for a `127.0.0.1` or `tailscale0` bind. Containers reach the LAN IP, so a LAN-interface bind still lets them in.
- **Candidates:** Pi-hole (idle since late Sept 2026; it could go loopback-only or stop) and go2rtc's WebRTC port.
- **Can't use it for:** SSH, SMB, HA, Netdata or node-exporter. They must stay on the LAN, or are scraped from a container.
- **Verdict:** not a fix. At most a small extra once option 2 is in.

## Recommended change (option 2)

### Rules

- **Chain:** `JESS-DOCKER-IN`, inserted at the top of `INPUT` for `-i br-+` and `-i docker0` only.
- **Return, not accept:** allowed packets `RETURN`, so they fall through to the existing chain (`ts-input`) and the `ACCEPT` policy.

```text
-A INPUT -i br-+    -j JESS-DOCKER-IN
-A INPUT -i docker0 -j JESS-DOCKER-IN

-A JESS-DOCKER-IN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
-A JESS-DOCKER-IN -i <br of gateway_nerves_gateway-net> -p tcp -m multiport --dports 8004,8053,8123,8888 -j RETURN
-A JESS-DOCKER-IN -i <br of monitoring_monitoring-net>  -p tcp -m multiport --dports 5050,9100,9400,19999 -j RETURN
-A JESS-DOCKER-IN -m limit --limit 6/min -j LOG --log-prefix "jess-docker-in: "
-A JESS-DOCKER-IN -p tcp -j REJECT --reject-with tcp-reset      # fail fast instead of hanging
-A JESS-DOCKER-IN -j DROP
```

The `ESTABLISHED,RELATED` rule keeps replies working: the host calling a container (`OUTPUT`) gets its answers back through `INPUT` from the bridge.

**Observe mode** swaps the last two lines for `-j RETURN`, so nothing is blocked and only the `LOG` line fires. The log lines carry addresses and ports only, never content.

### How it is applied

- **Resolved at apply time:** the bridge names (`br-<first 12 hex of the network id>`) come from the network names.
  - A network keeps its bridge name until it is recreated. The gateway-net bridge dates from 2026-06, monitoring-net from 2026-04.
- **A recreated network fails closed** (reject) until the next apply. To keep that short, the unit re-applies at boot and on every Docker network `create`/`destroy` event (`docker events` watcher, `Restart=always`).
- **Atomic:** the chain is refilled with one `iptables-restore --noflush` transaction.
- **Survives reboots** without iptables-persistent: the unit rebuilds the chain at boot.
- **Nothing else rewrites it:** Docker doesn't touch `INPUT`, and Tailscale only manages `ts-input`.
- **Small boot window:** the chain is in place a few seconds after `docker.service` starts.

### Rollout

1. Owner OK.
2. Install in **observe mode**. Leave it for a day, covering a night of jobs and backups, then read `journalctl -k -g jess-docker-in`. Any legitimate (network, port) pair goes into the allow-list.
3. Switch to **enforce**.
4. Re-run the probe. Expected result:
   - gateway-net reaches only 8004/8053/8123/8888;
   - monitoring-net reaches only 5050/9100/9400/19999;
   - every other network reaches nothing on the host;
   - DNS from containers still resolves (Docker resolver → router).
5. Verify the real paths:
   - orchestrator → HA (`/api/` with the token → 200, and one light command from chat);
   - Prometheus targets `node-exporter` (Jupiter), `netdata` and `conjure` up;
   - blackbox probes green;
   - Alertmanager → orchestrator webhook;
   - the next nightly backups;
   - a CI deploy;
   - a `docker compose build`.

### Rollback (any time, seconds)

```bash
sudo systemctl disable --now jess-docker-input.service   # ExecStopPost removes the jumps and the chain
```

Without the unit:

```bash
sudo iptables -D INPUT -i br-+ -j JESS-DOCKER-IN
sudo iptables -D INPUT -i docker0 -j JESS-DOCKER-IN
```

A reboot with the unit disabled also clears it.

## Not covered by this change

- **The allow-list is per network, not per container.** Every gateway-net member (frontend, radicale, actual-budget, redis, loki) gets the same four ports as the orchestrator. Narrowing it to the orchestrator needs a fixed orchestrator address (`ipv4_address` in compose). That can come later.
- **Published `0.0.0.0` ports** (8888, 2283, 13378) stay reachable from every container, through DNAT and `FORWARD`. They are LAN-facing by design anyway.
- **Container → LAN and container → internet** (`FORWARD`) are untouched. Any container can still reach Helios, the router and IoT devices. `DOCKER-USER` rules would be the next step, as a separate task.
- **The LAN and the tailnet still reach these host services**, since ufw is inactive. This change is about containers only.
- **promtail and Homepage mount the Docker socket.** A `:ro` bind does not stop API calls (tested: `GET /version` → 200 through a read-only mount), so both are root-equivalent on Jupiter. No firewall rule contains them.
- **`coder-net`** (see `CODER_PLAN.md`) is `isolated`, so it has no host address. It still needs its fixed bridge name to start with `br-`, so the chain covers it as the backstop the spike asks for.
