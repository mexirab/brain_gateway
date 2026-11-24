# Container → LAN reachability on Jupiter (2026-11-24)

**Status (2026-11-24):** measured; the fix is built and tested (commit on this branch, `selftest_netns.py` 37/37) but **not installed**. Resume notes: PR #181. The owner decides (see [Decisions for the owner](#decisions-for-the-owner)).

Follow-up to [`CONTAINER_HOST_REACHABILITY.md`](CONTAINER_HOST_REACHABILITY.md) (PR #176). That note covers traffic from containers to Jupiter **itself** (the `INPUT` path). This one covers traffic from containers to **everything off Jupiter**: the LAN, the router, the tailnet and the internet (the `FORWARD` path).

## Summary

- **Every container on a NAT network reaches the whole LAN and the tailnet.** That is 17 of the 19 Docker networks, i.e. every running container except Home Assistant and Pi-hole (host-networked). All 17 gave the same result. Reachable:
  - **the router:** its DNS, its admin page and its other LAN-side services. For its port-mapping settings, see [Decisions for the owner](#decisions-for-the-owner).
  - **the router's own services through its public address** too (NAT loopback). So blocking `10.0.0.1` alone would not hide them.
  - **Helios:** SSH, llama-server `:8080`, STT `:8003`, node-exporter, GPU exporter, NUT exporter, Netdata.
  - **other LAN devices:** about two dozen answered a short port sweep. Among them: Cast speakers (local HTTP API on 8008/8009), the voice satellites (ESPHome API on 6053, noise-encrypted), several web admin pages and SSH servers.
  - **tailnet peers, as Jupiter:** Docker masquerades the traffic, so the peer sees Jupiter's tailnet address. Saturn's SSH answered; two personal devices answered with a reset (the path works, the port is closed); MagicDNS `100.100.100.100` answered.
  - **Jupiter's own published ports through its LAN IP:** orchestrator 8888, Immich 2283, Audiobookshelf 13378, Paperless 8777, loki-push-gw 3101.
  - the internet.
- **The two internal networks** (`searxng-probe-net`, `coder-net`) reach nothing, as designed.
- **Who really uses the LAN:**
  - the orchestrator, Prometheus, blackbox-exporter and Alertmanager;
  - plus router DNS from the default `bridge` network, which ad-hoc `docker run` containers and **image builds** use.
  - In a 10-minute capture no content-handling container (searxng, Immich, Audiobookshelf, Paperless, Homepage, Grist) opened a single LAN or tailnet connection, and no container used the tailnet at all.
- **DNS:**
  - **On user-defined networks** (every compose network), Docker 29's resolver sends upstream queries from the host itself: captured, the queries leave `enp7s0` and never cross a bridge.
  - **On the default `bridge`** (`docker0`), containers get `nameserver 10.0.0.1` and query the router directly through `FORWARD`. That includes BuildKit's `RUN` steps (measured with a cache-only build).
- **A root container can borrow another container's network** (found in review, measured):
  - **Who:** a container with `NET_RAW` (in Docker's default capability set, so any root container that doesn't drop it, e.g. brain-frontend).
  - **How:** it sends a frame to a two-network neighbour's MAC address. Containers inherit `ip_forward = 1` from the host, so the neighbour's kernel routes the packet out of its own default-route network.
  - **Effect:** any rule keyed on that network's bridge lets the packet through. That includes PR #176's INPUT allow lines, e.g. orchestrator → HA.
  - **Also:** the same capability can forge a LAN source address, which Docker doesn't masquerade.
- **IPv6:** Jupiter doesn't forward IPv6 (`net.ipv6.conf.all.forwarding = 0`), so there is no IPv6 `FORWARD` path today.
- **Recommended fix:** a `FORWARD` allow-list for private destinations, as a third chain in `jess-docker-input` (PR #176) with its own observe/enforce switch.
  - Every Docker bridge is denied the LAN, the tailnet and Jupiter's published ports, except the few (network, destination, port) triples below. The internet stays open.
  - Packets whose source address doesn't belong to the bridge they arrive on are dropped, in both the new chain and `JESS-DOCKER-IN`.

## Method

- **Probe:** `scripts/container-firewall/probe_lan_reach.py`, a stdlib TCP-connect script (2 s timeout) plus single UDP queries (DNS `example.com A`, one NAT-PMP external-address request). Its docstring has the loop and what each result means.
- **Container:** one throwaway probe per Docker network (all 19): orchestrator image, `--cap-drop ALL`, `no-new-privileges`, `--rm`.
- **Targets:** the router, Helios, Saturn, Uranus, Jupiter's LAN IP (published ports), the voice satellites, three tailnet peers plus MagicDNS, the router's public address, and two internet hosts. The satellite, tailnet and public addresses were passed on the command line and are not written down here.
- **LAN sweep:** from one content network (`immich_default`), every address in `10.0.0.0/24` on ten common ports (22, 80, 443, 554, 1883, 6053, 8008, 8009, 8080, 9999). Counts only.
- **Real flows:** 10 minutes of `tcpdump` on Jupiter (headers only) of packets entering a Docker bridge for `10.0.0.0/24` or `100.64.0.0/10`. Source addresses were mapped back to the running containers; the probe containers' own packets were dropped.
- **DNS path:** `tcpdump` on the bridges and `enp7s0` during a lookup from a user-defined network, from `bridge`, and from a BuildKit `RUN` step (`docker buildx build --no-cache --output type=cacheonly`, no image kept).
- **Router:** SSDP discovery (UPnP) plus read-only NAT-PMP and PCP requests from the host, and the TLS certificate on the LAN and public addresses.
- **Config:** Jupiter's `.env` (addresses only), `monitoring/prometheus/prometheus.yml`, the Alertmanager render and every compose project's environment.
- **Review:** code-reviewer, security and prod-support read the first draft. Their checks on the box (resolver files, `ip_forward` inside containers, tailscaled's netfilter mode) are folded in.

## Findings: what a container reaches

Same result from all 17 NAT networks. "Reset" means the TCP connection was refused: the path works, only the port is closed.

| Target | Reachable | Needed by |
|---|---|---|
| Router `10.0.0.1` | DNS, web admin, other LAN-side services | DNS: containers on the default `bridge` and image builds. Nothing else. |
| Router public address (NAT loopback) | the router's web service | nobody |
| Helios `10.0.0.195` | open: 22, 8003, 8080, 9100, 9199, 9400, 19999; reset: 8004, 9080, 10300, 10301 | orchestrator 8003, 8080 (8004 in the TTS rollback); Prometheus 8080, 9100, 9199, 9400, 19999; blackbox 8003, 8080 |
| Saturn `10.0.0.58` | (down on the LAN since 2026-11-13: timeouts) | orchestrator 8053 (`PIHOLE_URLS`); Prometheus 9100, 9400, 19999; Alertmanager + Prometheus 9466 (dead-man's switch, not installed) |
| Uranus `10.0.0.173` | (unreachable: timeouts) | Prometheus 9100, 9400, 19999 |
| Jupiter LAN IP, published ports | open: 2283, 3101, 8777, 8888, 13378 | orchestrator 8777 (`PAPERLESS_URL`), 8888; Prometheus + blackbox + Alertmanager 8888; blackbox 8777 |
| Voice satellites | open: 6053 | orchestrator (`VOICE_SATELLITE_DEVICES`) |
| Other LAN devices (sweep) | about two dozen: Cast 8008/8009, web admin 80/443/8080, SSH | nobody |
| Tailnet: Saturn | open: 22; reset: 9466 | nobody (the dead-man's switch uses Saturn's LAN address) |
| Tailnet: personal devices | reset: 22 | nobody |
| Tailnet: MagicDNS `100.100.100.100` | open: 53 | nobody |
| Internet (`1.1.1.1`, `9.9.9.9`) | 53, 80, 443; DNS answers | most containers |

**From the internal networks** (`searxng-probe-net`, `coder-net`): nothing. Every target fails at once (no route), and so does the resolver for outside names.

## Findings: the router

- **Port mapping (NAT-PMP, PCP, UPnP):** see [Decisions for the owner](#decisions-for-the-owner). The details went to the owner directly, not into this public file.
- **Admin page:** answers on the LAN address and, from inside, on the public address (same certificate).
- **Hairpin back to Jupiter:** the probed ports on the public address showed no forward to Jupiter. 8123 and 80 time out; if they were forwarded to Jupiter, HA would have answered. A forward or mapping that does exist would bring a container back to its target over `enp7s0`, where neither chain looks.

## What containers actually use

From the 10-minute capture and the DNS traces. The "Path" column says which chain sees it: a destination on Jupiter itself goes through `INPUT` (PR #176), not `FORWARD`.

| Container (leaves via) | Destination | Path | Why |
|---|---|---|---|
| orchestrator (`gateway-net`; `host-access` after PR #176) | Helios 8080 | FORWARD | `MODEL_URL`, `VISION_MODEL_URL`, `FALLBACK_MODEL_URL` |
| | voice satellites 6053 | FORWARD | ESPHome API (connects out) |
| | Jupiter 8123 | INPUT | `HA_URL` |
| | LAN clients, high ports | FORWARD, reply | answers to Cast speakers and phones fetching from `:8888` |
| Prometheus (`monitoring-net`; `host-scrape` after PR #176) | Helios 8080, 9100, 9199, 9400, 19999 | FORWARD | scrape jobs `llama-primary`, `node-exporter`, `nut`, `gpu-exporter`, `netdata` |
| | Saturn, Uranus 9100, 9400, 19999 | FORWARD | the same jobs (both down: SYN retries) |
| | Jupiter 8888 | FORWARD (published port) | job `orchestrator` |
| | Jupiter 5050, 9100, 9400, 19999 | INPUT | Jupiter's own exporters |
| blackbox-exporter (`frontend-probe-net`; `host-scrape` after PR #176) | Helios 8003, 8080 | FORWARD | STT + brain `/health` probes |
| | Jupiter 8777, 8888 | FORWARD (published port) | Paperless + orchestrator probes |
| | Jupiter 5050, 8004 | INPUT | Conjure + VoxCPM probes |
| loki-push-gw | Helios, high port | FORWARD, reply | answers to Helios promtail's pushes |
| default `bridge`: `docker run` containers (the throwaway test container in `CLAUDE.md`, this probe), BuildKit `RUN` steps (`pip`, `apt`, `npm`, the frontend's build-time font download) | router 53 (udp + tcp) | FORWARD | `nameserver 10.0.0.1` from the host's `/etc/resolv.conf` |

**Needed but not seen in those 10 minutes:**
- **From the config:**
  - orchestrator → Helios 8003 (STT, voice turns only);
  - orchestrator → Saturn 8053 (Saturn is down);
  - orchestrator → Jupiter 8777 (Paperless tools);
  - Alertmanager → Jupiter 8888 (self-audit webhook);
  - Alertmanager → Saturn 9466 (dead-man's switch, not installed).
- **Only in a rollback or by hand:**
  - Helios 8004 for the orchestrator and blackbox, when the TTS rollback in `tts/llama-server-primary.service` moves the voice back to Helios (8002 for the older Qwen-TTS rollback);
  - flashing a satellite with the ESPHome container (`docs/VOICE_AND_TTS.md`): it needs the device's OTA and API ports;
  - `scripts/tts_compare.py` against Helios 8004.
- **Not running, would need lines if started:** the Wyoming bridges (`gateway-net`, Helios 8003); `nebula-sync` (Saturn 8053); ntfy (`NTFY_URL`, off).
- **Unverified (settings stored in the apps' databases):** an Immich remote machine-learning URL, a Paperless mail account on a LAN host. Observe mode would show either.

**Not needed:** Helios SSH. `MODEL_SSH_USER` is unset, so the orchestrator never SSHes to start the model.

Everything else talks container-to-container by name, or to the internet (Google, Pushover, Gmail, the search engines, Context7, Immich and Audiobookshelf metadata).

## Options

### A. Deny the LAN to the content networks only

- **Rule:** drop private destinations from `searxng-net`, `immich_default`, `audiobookshelf_default`, `paperless_default` and `homepage_default`.
- **Pro:** small, and nothing the orchestrator, monitoring or builds do can break.
- **Con:** a deny-list.
  - Every network not on it keeps the LAN: `gateway-net` (radicale, actual-budget, redis, loki, the frontend), `monitoring-net` (grafana, promtail), `grist-net`, and `bridge`.
  - A new compose project gets the LAN by default.
  - It is the opposite of PR #176, which allow-lists.

### B. Allow-list private destinations for every bridge (recommended)

- **Rule:** traffic from any Docker bridge to a private destination is denied, unless a `forward` line allows that (network, destination, protocol, port). The internet stays open.
  - **Private destinations:** `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (tailnet and MagicDNS), `169.254.0.0/16`, `192.0.0.0/24` (PCP's anycast address). Plus every connection Docker DNATs to a published port, through any host address.
  - Traffic to another Docker bridge that isn't DNAT'd is left to Docker's own isolation rules.
- **Pro:** new networks are closed by default; matches PR #176; one place to read what reaches the LAN.
- **Con:** a missed legitimate flow breaks something in enforce mode. Observe mode exists to find those first.
- **Network-granular, like PR #176:** a line allows a whole network.
  - `host-access` and `host-scrape` (one or two members each) carry the orchestrator's and Prometheus's lines.
  - Alertmanager has no dedicated network, so its two lines go on `monitoring-net`. That gives grafana, promtail, loki, radicale and actual-budget the same two destinations: the orchestrator's API, which they already reach by name, and Saturn's dead-man's port. A dedicated `gw_priority` network for Alertmanager would remove that; optional.

### C. Docker settings alone

- Docker 29 has no per-network egress filter. Only `internal: true` exists, and that takes away the internet these containers need.
- Docker's nftables firewall backend has no `DOCKER-USER` chain, and Jupiter uses the default iptables backend. If that ever changes, this design needs a rewrite.

### Where the chain lives

| | Extend `jess_docker_input.py` (recommended) | Separate unit |
|---|---|---|
| Code | ~200 more lines; reuses network → bridge resolution, digest/drift repair, jump placement, the watcher, metrics, `summary` | its own copy of all that, or a shared module (both scripts are single-file installs today) |
| Host install | the same unit and config file; the upgrade is the PR #176 upgrade steps | a second unit, config, metrics file and alert set |
| Failure isolation | one process: a config error blocks both (both then keep their current rules, never empty) | independent |
| Rollout | `forward-mode off/observe/enforce`, separate from the INPUT `mode`: INPUT can go to enforce while FORWARD still observes | separate by construction |
| Source check | one rule set for both chains | duplicated |

Extending wins. The only real cost is the shared failure domain, and that failure mode is "rules stay as they were".

## Proposed rules (option B)

### Config: new lines in `/etc/jess-docker-input.conf`

```text
forward-mode observe    # off | observe | enforce. A missing line = off, so today's config keeps working.

# forward <docker network> <address>[/<len>] <tcp|udp> <port>[,<port>...]

# Default bridge: ad-hoc `docker run` containers and image builds (BuildKit RUN
# steps) resolve through the router (nameserver 10.0.0.1 from /etc/resolv.conf).
forward bridge 10.0.0.1 udp 53
forward bridge 10.0.0.1 tcp 53

# orchestrator (host-access)
forward gateway_nerves_host-access 10.0.0.195 tcp 8003,8004,8080   # Helios: STT_URL, VoxCPM (TTS rollback), llama-server (MODEL/VISION/FALLBACK_MODEL_URL)
forward gateway_nerves_host-access <satellite IP> tcp 6053          # one line per voice satellite (VOICE_SATELLITE_DEVICES)
forward gateway_nerves_host-access 10.0.0.58 tcp 8053               # Saturn Pi-hole (PIHOLE_URLS)
forward gateway_nerves_host-access 10.0.0.248 tcp 8777,8888         # Paperless + own API via Jupiter's LAN IP (published ports)

# Prometheus + blackbox-exporter (host-scrape)
forward monitoring_host-scrape 10.0.0.195 tcp 8003,8004,8080,9100,9199,9400,19999
forward monitoring_host-scrape 10.0.0.58 tcp 9100,9400,9466,19999
forward monitoring_host-scrape 10.0.0.173 tcp 9100,9400,19999
forward monitoring_host-scrape 10.0.0.248 tcp 8777,8888

# Alertmanager (monitoring-net is its only network)
forward monitoring_monitoring-net 10.0.0.248 tcp 8888               # self-audit webhook
forward monitoring_monitoring-net 10.0.0.58 tcp 9466                # dead-man's switch heartbeat (once installed)

# TRANSITION: until PR #176's compose change is deployed, the orchestrator
# leaves through gateway-net, Prometheus through monitoring-net and
# blackbox-exporter through frontend-probe-net. Delete before enforce.
forward gateway_nerves_gateway-net 10.0.0.195 tcp 8003,8004,8080
forward gateway_nerves_gateway-net <satellite IP> tcp 6053
forward gateway_nerves_gateway-net 10.0.0.58 tcp 8053
forward gateway_nerves_gateway-net 10.0.0.248 tcp 8777,8888
forward monitoring_monitoring-net 10.0.0.195 tcp 8080,9100,9199,9400,19999
forward monitoring_monitoring-net 10.0.0.58 tcp 9100,9400,19999
forward monitoring_monitoring-net 10.0.0.173 tcp 9100,9400,19999
forward gateway_nerves_frontend-probe-net 10.0.0.195 tcp 8003,8004,8080
forward gateway_nerves_frontend-probe-net 10.0.0.248 tcp 8777,8888
```

- **Nothing allows the tailnet,** nor the router except DNS from `bridge`.
- **Config checks:** the watcher refuses a prefix shorter than `/24`, and a router line on any port but 53.
- **Satellites:** the repo copy keeps the `<satellite IP>` placeholder; the `/etc` copy has the real addresses.
- **Exact address:** a line matches the address the container asked for. A container that reaches a published port as `host.docker.internal` or through the tailnet IP needs its own line. No current consumer does that.

### Chains

`JESS-DOCKER-FWD` (IPv4) and `JESS-DOCKER-FWD6` (IPv6), jumped from `FORWARD` positions 1–2 for `-i br-+` and `-i docker0` only. LAN → container traffic (a phone opening Immich) arrives on `enp7s0` and never enters them.

**Why the top of `FORWARD` and not `DOCKER-USER`:**
- tailscaled (netfilter mode on) re-inserts its `-j ts-forward` at `FORWARD` position 1 on every start, and `ts-forward` ends with `-o tailscale0 -j ACCEPT`.
- Below that, every container → tailnet packet would be accepted before a `DOCKER-USER` rule ran, with no counter or alert.
- PR #176 already handles the same behaviour on `INPUT`; the watcher keeps these jumps on top the same way.
- **The cost:** a dockerd restart can push them down for up to a minute, until the next sync repairs them.

```text
-A JESS-DOCKER-FWD -m conntrack --ctstate RELATED,ESTABLISHED --ctdir REPLY -j RETURN    # answers to LAN clients of published ports
-A JESS-DOCKER-FWD -i <bridge> ! -s <its subnet> -j JESS-DOCKER-SPOOF                     # one per bridge network
-A JESS-DOCKER-FWD -m conntrack --ctstate DNAT -j JESS-DOCKER-FWD-LAN                    # a published port, through any host address
-A JESS-DOCKER-FWD -o br-+ -j RETURN                                                     # container -> container: Docker's isolation decides
-A JESS-DOCKER-FWD -o docker0 -j RETURN
-A JESS-DOCKER-FWD -d 10.0.0.0/8 -j JESS-DOCKER-FWD-LAN                                  # likewise 172.16/12, 192.168/16,
                                                                                          # 100.64/10, 169.254/16, 192.0.0.0/24
#  (anything else = the internet: RETURN to FORWARD, then Docker's rules as today)

-A JESS-DOCKER-FWD-LAN -m conntrack --ctstate INVALID -j <counter; DROP in enforce>
-A JESS-DOCKER-FWD-LAN -i <bridge> -p <tcp|udp> -m conntrack --ctorigdst <addr> --ctorigdstport <port> -j RETURN   # one per forward port
-A JESS-DOCKER-FWD-LAN -m comment --comment "jess: fwd unmatched"                         # counter
-A JESS-DOCKER-FWD-LAN <per-flow + global rate limit> -j LOG --log-prefix "jess-docker-fwd observe: "
-A JESS-DOCKER-FWD-LAN -j REJECT --reject-with icmp-admin-prohibited                      # enforce only

-A JESS-DOCKER-SPOOF <counter, rate-limited LOG "jess-docker-spoof observe: ">; DROP in enforce

-A JESS-DOCKER-FWD6 -m conntrack --ctstate RELATED,ESTABLISHED --ctdir REPLY -j RETURN
-A JESS-DOCKER-FWD6 <counter, rate-limited LOG>; DROP in enforce                          # no Docker network has IPv6
```

**How the rules work:**
- **Original destination:** the allow lines match `--ctorigdst`/`--ctorigdstport`, the address and port the container asked for, so a published port matches by the address it was reached at. For Paperless, the container asks for `10.0.0.248:8777`, which Docker DNATs to the container's port 8000.
- **Every packet is checked,** not only the first. So in enforce a connection opened during observe, or to a port later removed from the list, is cut, as in `JESS-DOCKER-IN`.
- **Source check:**
  - A packet on bridge X whose source isn't in X's subnet came from a forged address, or was routed through a two-network container. It is dropped in enforce.
  - It sits after the reply rule. That matters for a two-network container that answers a published-port client from the other network's address: such asymmetric replies still pass.
  - **The same rule belongs at the top of `JESS-DOCKER-IN` (PR #176),** whose allow lines the same trick bypasses.
  - The watcher already reads every network's subnets from `docker network inspect`.
- **`-o br-+ RETURN`:** Docker 29 itself drops a packet aimed at another network's container address. Its raw `PREROUTING` rules drop each container address arriving from a foreign interface, and its `DOCKER` chain drops `! -i br-x -o br-x`.
- **Reject with ICMP "administratively prohibited":**
  - the app fails at once instead of waiting for a timeout;
  - the probe can tell it apart from the target's own reset.
  - **But** the kernel sends only a few ICMP errors per second to one container, so in a burst most rejects look like timeouts. The probe's `--recheck` handles that.
- **Syntax checked** with iptables-nft 1.8.10 (Jupiter's) in a throwaway network namespace, including `--ctorigdstport`.

### What the watcher would add

- **Rendering:**
  - resolve `forward` lines to bridges the same way as `allow` lines. A missing network gets no rule, so it fails closed in enforce;
  - render each line's protocol;
  - render the per-bridge source rules from the networks' subnets.
- **Jumps:**
  - keep the two `FORWARD` jumps (IPv4 and IPv6) at positions 1–2, and repair a displaced jump or an edited chain, as for `INPUT`;
  - never touch `DOCKER-USER`, `ts-forward` or Docker's chains.
- **New problems that set `check_ok = 0`** (the existing `ContainerFirewallCheckFailing` alert then fires):
  - a network driver other than `bridge`, `host` or `null` (macvlan/ipvlan bypass `FORWARD`);
  - a host-networked container other than Home Assistant and Pi-hole;
  - `net.ipv6.conf.all.forwarding = 1` while `JESS-DOCKER-FWD6` is missing.
- **Metrics:**
  - `jess_docker_input_forward_enforcing`;
  - `jess_docker_input_forward_unmatched_packets_total{network}` and `..._forward_logged_packets_total`;
  - `jess_docker_input_spoofed_packets_total{chain}`;
  - a per-allow-line packet count, so "orchestrator cut off from Helios" is visible. To the app it looks like "brain asleep" and would trigger a pointless wake.
- **Alerts:**
  - `ContainerFirewallForwardRejecting` and `ContainerFirewallSpoofedPackets` (quiet);
  - a FORWARD twin of `ContainerFirewallStillObserving`;
  - all added to the `ContainerFirewall*` matcher in `alertmanager.yml.template` (`test_alert_routing.py` fails otherwise).
- **`summary`** also groups `jess-docker-fwd` and `jess-docker-spoof` log lines by network, destination and port.
- **Optional `check --forward-coverage`:** list every private host:port in `.env` and the rendered Prometheus/Alertmanager configs that no `forward` line covers. That would have caught the bridge DNS and the TTS rollback ports.

## Rollout and rollback

1. **Build + review + unit tests** (repo only). Plus a functional test in throwaway network namespaces on Jupiter: a fake "container", "two-network container", "LAN host", "router" and "tailnet" interface; the real rendered rules, in observe and enforce. It covers:
   - a forged source;
   - a packet routed through the two-network container;
   - a tailscaled-style `ts-forward` jump inserted above ours.
   - Then the rollback (`forward-mode off`) must leave the namespace's tables exactly as before.
2. **Install in observe** (owner OK): the PR #176 upgrade steps, plus `forward-mode observe` and the `forward` lines in `/etc/jess-docker-input.conf`.
   - **Verify:**
     - the probe on every network still shows everything reachable (observe blocks nothing);
     - `summary` lists the probe's packets;
     - the orchestrator, Prometheus, blackbox and `bridge` DNS rules count packets;
     - every rule outside `JESS-DOCKER-*` and the jumps is unchanged (diff of `iptables-save`/`ip6tables-save`).
3. **Observe at least 24 h.** The window must contain:
   - the windows from PR #176: the 07:00 self-audit plus one alert webhook, a CI deploy, Paperless running;
   - **one uncached image build** (`docker build --no-cache` of the orchestrator image) and **one throwaway test-container run** (both use router DNS on `bridge`);
   - Helios awake for part of the window, with a voice turn (STT) and a Paperless tool call.
   - Read `bridge` rows in `summary` as real traffic, not probe noise.
4. **Before enforce:**
   - PR #176's compose change is deployed and the `TRANSITION` lines are gone;
   - the satellite flashing command in `docs/VOICE_AND_TTS.md` uses `--network host`;
   - the TTS rollback headers mention the firewall (8002 needs a line; 8004 already has one).
5. **Enforce:** `forward-mode enforce` + reload, then the probe with `--recheck`. Only the allow-listed (network, destination, port) triples should still answer.

**Rollback:**
- **FORWARD only, seconds:** `forward-mode off` + `sudo systemctl reload jess-docker-input`. That removes `JESS-DOCKER-FWD*`/`-SPOOF` and their `FORWARD` jumps and leaves `INPUT` as it is.
- **If the watcher itself is broken:** `sudo iptables -D FORWARD -i br-+ -m comment --comment jess-docker-input -j JESS-DOCKER-FWD`, the same for `-i docker0` and for `ip6tables` with `JESS-DOCKER-FWD6`. The runbook will spell out each command.
- **Everything:** `sudo systemctl disable --now jess-docker-input` (PR #176's rollback).
- **Symptoms in enforce:**
  - "Temporary failure in name resolution" in a build: the `bridge` DNS lines;
  - "No route to host" in an app.
  - Either way, `sudo jess-docker-input summary --hours 1` names the flow.

## Not covered

- **Internet egress stays open** for every NAT network. A content container can still send data out. Limiting that needs per-container egress proxies or destination lists, which is a different project.
- **The router's public address:**
  - Containers keep reaching the router's own services through NAT loopback, and any port forward or mapping on it.
  - Closing that needs the public address in the chain, and it is dynamic. The watcher could learn it locally (tailscaled's endpoint list, `tailscale status --json`) into an ipset, keeping the last value when a lookup fails. That is a possible follow-up.
  - Turning off NAT loopback on the router also works, if nothing needs it.
- **Host processes** (tailscaled included) aren't filtered; the chains only see containers.
- **Network-granular,** as in PR #176: a container added to `host-access`, `host-scrape` or `monitoring-net` inherits those lines.
- **The source check stops borrowing, but the capability stays.** Cheaper and complementary, as follow-ups:
  - `sysctls: net.ipv4.ip_forward=0` on every container that sits on more than one network;
  - `cap_drop: [ALL]` (and a non-root user) for brain-frontend and blackbox-exporter, which need no raw sockets;
  - at least drop `NET_RAW` for immich_server, audiobookshelf and homepage.
- **A published port reached from the network it is published on** isn't DNAT'd. Docker's DNAT rule skips that bridge, so docker-proxy answers it on `INPUT` (PR #176's chain). Reached from any other network, through any host address (LAN IP, bridge gateway, tailnet IP), it is DNAT'd and this chain sees it.
- **Boot window:** containers have the full LAN for the few seconds between dockerd starting and the watcher's first sync, as with PR #176.
- **promtail and Homepage mount the Docker socket** (root-equivalent, see PR #176). No firewall rule contains them.
- **Tailnet policy:** containers reach the personal tailnet devices as Jupiter. A Tailscale ACL that stops Jupiter from opening connections to personal devices would also cover a compromised host, which no Jupiter firewall can.

## Decisions for the owner

1. **Router (now, independent of everything else):** check the router's automatic port-mapping settings (UPnP, NAT-PMP, PCP) and remote management. The details went to you directly, not into this public file. Turn port mapping off unless something needs it: Tailscale works without it, at worst over a relay more often.
2. **Option B (allow-list every bridge) or A (deny the five content networks only)?** Recommended: B.
3. **Build it into `jess-docker-input`, or as a separate unit?** Recommended: into `jess-docker-input`, with the source check added to `JESS-DOCKER-IN` in the same change.
4. **Then: build, test, and install in observe mode** (a host change: the upgrade steps above)?
