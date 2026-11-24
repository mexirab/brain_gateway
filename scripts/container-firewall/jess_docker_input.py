#!/usr/bin/env python3
"""Limit what Docker containers on Jupiter can reach: host ports (INPUT) and private destinations (FORWARD).

Why this exists
---------------
Measured 2026-11-21 (docs/internal/CONTAINER_HOST_REACHABILITY.md): every
container on an ordinary Docker network could open connections to every Jupiter
service listening on all interfaces (SSH, SMB, Pi-hole, Home Assistant, Netdata,
node-exporter, ...), through any host address. Measured 2026-11-24
(docs/internal/CONTAINER_LAN_REACHABILITY.md): the same containers also reached
the router, Helios, the other LAN devices and the tailnet. ufw is inactive, and
Docker's `isolated` gateway mode is refused for networks with internet access.
So the fix is on the host: packets that arrive from a Docker bridge go through
our chains, which let the needed pairs through and log (observe) or block
(enforce) the rest.

What it does
------------
INPUT (container -> Jupiter itself), always on:
- Chains JESS-DOCKER-IN (iptables) and JESS-DOCKER-IN6 (ip6tables), jumped from
  the top of INPUT for `-i br-+` and `-i docker0` ONLY. Loopback, LAN and
  tailnet traffic never enters them, so they cannot lock out SSH. Packets that
  pass `RETURN` to INPUT (policy ACCEPT, `ts-input` still runs). The jumps are
  kept at INPUT positions 1-2 (tailscaled re-inserts its jump at the top when it
  restarts).
- IPv4 rule order: replies to connections the HOST opened (`--ctdir REPLY`) pass;
  the source check (below); conntrack INVALID and non-unicast packets are
  dropped silently (passed in observe mode); then the `allow` lines, checked on
  every packet of a container-opened connection (so a connection opened during
  observe, or to a port later removed from the list, is cut in enforce); then
  the rest is counted, logged (per-flow + global rate limit, kernel header fields
  only) and, in enforce mode, rejected (TCP reset) or dropped.
- IPv6: no Docker network here has IPv6, but every bridge has a host link-local
  address and the host's services listen on `[::]`, so a container with
  NET_RAW (in Docker's default capability set) could reach them with hand-made
  packets. JESS-DOCKER-IN6 has no allow lines: host replies pass, the rest is
  counted, logged and (enforce) dropped.

FORWARD (container -> anything off the box), with its own `forward-mode`:
- Chains JESS-DOCKER-FWD (+ JESS-DOCKER-FWD-LAN, JESS-DOCKER-FWD-SPOOF) and
  JESS-DOCKER-FWD6, jumped from the top of FORWARD for `-i br-+`/`-i docker0`.
  Not from DOCKER-USER: tailscaled re-inserts `-j ts-forward` (which ends in
  `-o tailscale0 -j ACCEPT`) at FORWARD 1 on every start, so a hook below it
  would never see container -> tailnet traffic. Kept on top like the INPUT jumps.
- Private destinations (PRIVATE_V4: LAN, tailnet, link-local, PCP anycast) and
  every connection Docker DNAT'd to a published port go to JESS-DOCKER-FWD-LAN:
  the `forward` lines (matched on the ORIGINAL destination + port, via
  conntrack), else counted, logged and (enforce) rejected with ICMP
  admin-prohibited. The internet and container -> container traffic are left to
  Docker's rules. Replies to connections opened TOWARD a container pass.
- IPv6: forwarding is off on Jupiter. JESS-DOCKER-FWD6 drops (enforce) whatever
  a bridge sends, should it ever be turned on.
- `forward-mode off` (also when the line is missing) removes these chains.

Source check (both paths, IPv4): a packet on bridge X whose source address is
not in X's subnet was forged, or routed through a container on two networks
(containers inherit ip_forward=1). It would otherwise match X's allow lines.
Such packets are counted and logged, and dropped in enforce (INPUT `mode` /
`forward-mode`). Replies (`--ctdir REPLY`) are exempt, so a two-network
container answering from its other address still works.

Names
-----
`allow <network> <tcp|udp> <ports>` and `forward <network> <addr>[/len] <tcp|udp>
<ports>` lines name Docker networks. The bridge interface of each is looked up
at apply time (the network's `com.docker.network.bridge.name`, else
`br-<first 12 hex of the id>`). A network that doesn't exist (yet) gets no rule:
it fails closed in enforce mode until it exists and the next sync runs. A packet
arrives on the bridge of the network the container's DEFAULT ROUTE uses, so the
allow-listed networks are dedicated ones that carry the default route of their
(only) members via compose `gw_priority`: host-access (orchestrator) and
host-scrape (Prometheus, blackbox-exporter).

Each hook's chains are refilled in one `*-restore --noflush` transaction (never
without --noflush: that would wipe Docker's own rules). The main chain's first
rule carries a hash of the rendered ruleset; drift (a flushed or edited chain, a
removed or displaced jump, a recreated network) is detected and repaired every
60 s without touching a healthy chain.

Commands
--------
  render   print the rulesets for the current config + Docker networks (no changes)
  check    validate the config and reach dockerd; one line, exit 0/2/3 (unit ExecReload)
  apply    apply once
  watch    sync, then again on Docker network create/destroy events, on SIGHUP
           (re-reads the config) and every 60 s (drift check). Used by the unit.
  remove   delete every jump and chain (= no filtering). With
           --only-if-clean-stop (unit ExecStopPost) it does nothing after a crash,
           so a crash-restart keeps the rules in place.
  status   print the script's sha256 and every chain with packet counters
  summary  group the chains' kernel log lines (last --hours, default 24) by
           path, network, address, port

Metrics (--metrics-dir, a node_exporter textfile directory; `watch` only)
-------------------------------------------------------------------------
jess_docker_input.prom, rewritten every 60 s; see METRICS below.

Safety
------
Never flushes anything but its own chains. A bad config or an unreachable
dockerd leaves the chains as they are (log + retry), never empties them. Stdlib
only; runs as root on the host (the iptables and docker CLIs), never inside a
container.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DOCKER = "/usr/bin/docker"
JOURNALCTL = "/usr/bin/journalctl"
ROUTE_FILE = Path("/proc/net/route")
JUMP_COMMENT = "jess-docker-input"
JUMPS = (("-i", "docker0"), ("-i", "br-+"))
UNMATCHED_COMMENT = "jess: unmatched"
LOGGED_COMMENT = "jess: logged"
FWD_UNMATCHED_COMMENT = "jess: fwd unmatched"
FWD_LOGGED_COMMENT = "jess: fwd logged"
SPOOFED_COMMENT = "jess: spoofed"
SPOOF_LOGGED_COMMENT = "jess: spoof logged"
SPOOF_TAG = "jess-docker-spoof"
MODES = ("observe", "enforce")
FORWARD_MODES = ("off", "observe", "enforce")
PROTOS = ("tcp", "udp")
MAX_PORTS_PER_RULE = 15  # iptables multiport limit (kept for forward lines too)
MIN_FORWARD_PREFIX = 24
# Where a `forward` line may point, and what the FORWARD chain checks: the LAN,
# the tailnet + MagicDNS (100.64/10), link-local, PCP's anycast range.
PRIVATE_V4 = tuple(
    ipaddress.IPv4Network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "192.0.0.0/24")
)
OK_DRIVERS = ("bridge", "host", "null")  # anything else (macvlan, ipvlan, ...) bypasses our chains
CHECK_INTERVAL_S = 60.0
METRICS_FILE = "jess_docker_input.prom"
_NETWORK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_IFACE_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}")
_DEST_RE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?")
_DIGEST_RE = re.compile(r'jess-docker-input ([0-9a-f]{12})"')
_KLOG_RE = re.compile(
    r"(jess-docker-(?:in6?|fwd6?|spoof)) (observe|reject): .*?IN=(\S+) .*?DST=(\S+) .*?PROTO=(\S+)(?: .*?DPT=(\d+))?"
)
_KLOG_PATH = {
    "jess-docker-in": "in",
    "jess-docker-in6": "in6",
    "jess-docker-fwd": "fwd",
    "jess-docker-fwd6": "fwd6",
    SPOOF_TAG: "spoof",
}


@dataclass(frozen=True)
class Hook:
    """One set of our chains: `chain` (+ `subchains`), jumped from `builtin` for Docker bridges."""

    name: str  # address family: ipv4 | ipv6 (metrics label)
    iptables: str
    restore: str
    chain: str
    log_tag: str  # kernel log prefix
    builtin: str = "INPUT"
    subchains: tuple[str, ...] = ()
    hashname: str = ""  # hashlimit table name (<= 15 chars); default: log_tag

    @property
    def label(self) -> str:
        return self.name if self.builtin == "INPUT" else f"{self.name}-fwd"

    @property
    def chains(self) -> tuple[str, ...]:
        return (self.chain, *self.subchains)


Family = Hook  # the INPUT hooks were called families before the FORWARD ones existed

IN_SPOOF = "JESS-DOCKER-IN-SPOOF"
FWD_LAN = "JESS-DOCKER-FWD-LAN"
FWD_SPOOF = "JESS-DOCKER-FWD-SPOOF"
_IPT, _IPT_R = "/usr/sbin/iptables", "/usr/sbin/iptables-restore"
_IP6T, _IP6T_R = "/usr/sbin/ip6tables", "/usr/sbin/ip6tables-restore"
IPV4 = Hook("ipv4", _IPT, _IPT_R, "JESS-DOCKER-IN", "jess-docker-in", subchains=(IN_SPOOF,))
IPV6 = Hook("ipv6", _IP6T, _IP6T_R, "JESS-DOCKER-IN6", "jess-docker-in6")
FWD4 = Hook("ipv4", _IPT, _IPT_R, "JESS-DOCKER-FWD", "jess-docker-fwd", "FORWARD", (FWD_LAN, FWD_SPOOF))
FWD6 = Hook("ipv6", _IP6T, _IP6T_R, "JESS-DOCKER-FWD6", "jess-docker-fwd6", "FORWARD", hashname="jess-docker-fw6")
FAMILIES = (IPV4, IPV6)  # INPUT
FORWARD_HOOKS = (FWD4, FWD6)
HOOKS = FAMILIES + FORWARD_HOOKS

METRICS = {  # name: (type, help)
    "jess_docker_input_info": ("gauge", "Always 1; the sha256 label identifies the installed script."),
    "jess_docker_input_started_timestamp_seconds": ("gauge", "When the watcher process started."),
    "jess_docker_input_last_check_timestamp_seconds": ("gauge", "Last sync attempt (heartbeat, every 60 s)."),
    "jess_docker_input_last_apply_timestamp_seconds": ("gauge", "Last time a chain was (re)loaded."),
    "jess_docker_input_check_ok": ("gauge", "1 = last sync succeeded; 0 = config error, dockerd or iptables failed."),
    "jess_docker_input_enforcing": ("gauge", "1 = INPUT enforce mode, 0 = observe mode (as of the last good sync)."),
    "jess_docker_input_forward_active": ("gauge", "1 = the FORWARD chains are on (forward-mode observe or enforce)."),
    "jess_docker_input_forward_enforcing": ("gauge", "1 = forward-mode enforce (as of the last good sync)."),
    "jess_docker_input_networks_missing": ("gauge", "Allow- or forward-listed Docker networks that do not exist."),
    "jess_docker_input_networks_uncovered": (
        "gauge",
        "Networks the chains do not cover: bridge name, IPv6, not exactly one IPv4 subnet, or a non-bridge driver.",
    ),
    "jess_docker_input_unmatched_packets_total": (
        "counter",
        "Unicast packets from containers to the host that no rule allowed (resets when a chain is reloaded).",
    ),
    "jess_docker_input_logged_packets_total": ("counter", "Unmatched packets that were also logged (rate-limited)."),
    "jess_docker_input_forward_unmatched_packets_total": (
        "counter",
        "Packets from containers to private destinations that no forward line allowed (resets on a reload).",
    ),
    "jess_docker_input_forward_logged_packets_total": (
        "counter",
        "Forward-unmatched packets that were also logged (rate-limited).",
    ),
    "jess_docker_input_spoofed_packets_total": (
        "counter",
        "Packets from a bridge whose source is not in that bridge's subnet (forged or routed via another container).",
    ),
}


class ConfigError(ValueError):
    pass


class DockerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Allow:
    network: str
    proto: str
    ports: tuple[int, ...]


@dataclass(frozen=True)
class Forward:
    network: str
    dest: str  # "a.b.c.d" (a /32) or "a.b.c.d/len"
    proto: str
    ports: tuple[int, ...]


@dataclass(frozen=True)
class Config:
    mode: str
    allows: tuple[Allow, ...]
    forward_mode: str = "off"
    forwards: tuple[Forward, ...] = ()


@dataclass
class Networks:
    bridges: dict[str, str] = field(default_factory=dict)  # network name -> bridge interface
    uncovered: list[str] = field(default_factory=list)  # networks the chains can't see or check
    subnets: dict[str, str] = field(default_factory=dict)  # network name -> its one IPv4 subnet


@dataclass(frozen=True)
class Ruleset:
    family: Hook
    text: str
    digest: str
    rule_count: int  # all chains
    counts: tuple[tuple[str, int], ...] = ()  # (chain, rules) for each of family.chains


@dataclass(frozen=True)
class Rendered:
    rulesets: tuple[Ruleset, ...]
    missing: tuple[str, ...]
    absent: tuple[Hook, ...] = ()  # hooks that must not be installed (forward-mode off)


def log(msg: str) -> None:
    print(f"jess-docker-input: {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=check, timeout=60)


def script_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return "unknown"


def default_gateway() -> str | None:
    """The host's IPv4 default gateway (the router), or None if it can't be read."""
    try:
        for line in ROUTE_FILE.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError, struct.error):
        pass
    return None


# --------------------------------------------------------------------------- config


def _parse_ports(lineno: int, ports_s: str) -> tuple[int, ...]:
    if not re.fullmatch(r"[0-9]{1,5}(,[0-9]{1,5})*", ports_s):
        raise ConfigError(f"line {lineno}: ports must be numbers separated by commas")
    ports = tuple(sorted({int(p) for p in ports_s.split(",")}))
    if any(not 1 <= p <= 65535 for p in ports):
        raise ConfigError(f"line {lineno}: ports must be 1-65535")
    if len(ports) > MAX_PORTS_PER_RULE:
        raise ConfigError(f"line {lineno}: at most {MAX_PORTS_PER_RULE} ports per line")
    return ports


def _parse_dest(lineno: int, dest: str, ports: tuple[int, ...], gateway: str | None) -> str:
    if not _DEST_RE.fullmatch(dest):
        raise ConfigError(f"line {lineno}: destination must be an IPv4 address or network (a.b.c.d[/len])")
    try:
        net = ipaddress.IPv4Network(dest, strict=True)
    except ValueError:
        raise ConfigError(f"line {lineno}: bad destination (host bits set, or not an IPv4 address)") from None
    if net.prefixlen < MIN_FORWARD_PREFIX:
        raise ConfigError(f"line {lineno}: destination prefix must be /{MIN_FORWARD_PREFIX} or longer")
    if not any(net.subnet_of(p) for p in PRIVATE_V4):
        raise ConfigError(f"line {lineno}: not a private destination (the internet is not filtered)")
    if gateway is not None and ipaddress.IPv4Address(gateway) in net and set(ports) - {53}:
        raise ConfigError(f"line {lineno}: the router ({gateway}) may only be allowed on port 53 (DNS)")
    return str(net.network_address) if net.prefixlen == 32 else str(net)


def parse_config(text: str, gateway: str | None = None) -> Config:
    mode: str | None = None
    forward_mode: str | None = None
    allows: list[Allow] = []
    forwards: list[Forward] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "mode":
            if len(parts) != 2 or parts[1] not in MODES:
                raise ConfigError(f"line {lineno}: expected 'mode observe|enforce'")
            if mode is not None:
                raise ConfigError(f"line {lineno}: mode given twice")
            mode = parts[1]
        elif parts[0] == "forward-mode":
            if len(parts) != 2 or parts[1] not in FORWARD_MODES:
                raise ConfigError(f"line {lineno}: expected 'forward-mode off|observe|enforce'")
            if forward_mode is not None:
                raise ConfigError(f"line {lineno}: forward-mode given twice")
            forward_mode = parts[1]
        elif parts[0] == "allow":
            if len(parts) != 4:
                raise ConfigError(f"line {lineno}: expected 'allow <network> <tcp|udp> <port>[,<port>...]'")
            _, network, proto, ports_s = parts
            if not _NETWORK_RE.fullmatch(network):
                raise ConfigError(f"line {lineno}: bad network name")
            if proto not in PROTOS:
                raise ConfigError(f"line {lineno}: protocol must be tcp or udp")
            allows.append(Allow(network, proto, _parse_ports(lineno, ports_s)))
        elif parts[0] == "forward":
            if len(parts) != 5:
                raise ConfigError(
                    f"line {lineno}: expected 'forward <network> <address>[/<len>] <tcp|udp> <port>[,<port>...]'"
                )
            _, network, dest, proto, ports_s = parts
            if not _NETWORK_RE.fullmatch(network):
                raise ConfigError(f"line {lineno}: bad network name")
            if proto not in PROTOS:
                raise ConfigError(f"line {lineno}: protocol must be tcp or udp")
            ports = _parse_ports(lineno, ports_s)
            forwards.append(Forward(network, _parse_dest(lineno, dest, ports, gateway), proto, ports))
        else:
            raise ConfigError(f"line {lineno}: unknown keyword {parts[0]!r}")
    if mode is None:
        raise ConfigError("no 'mode' line")
    return Config(mode, tuple(allows), forward_mode or "off", tuple(forwards))


def load_config(path: Path) -> Config:
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    return parse_config(text, default_gateway())


# --------------------------------------------------------------------------- docker


def _one_subnet(net: dict) -> str | None:
    """The network's IPv4 subnet if it has exactly one, else None."""
    configs = (net.get("IPAM") or {}).get("Config") or []
    found: list[str] = []
    for c in configs if isinstance(configs, list) else []:
        sub = (c or {}).get("Subnet") if isinstance(c, dict) else None
        if not sub or ":" in str(sub):
            continue  # IPv6, or no subnet
        try:
            found.append(str(ipaddress.IPv4Network(str(sub), strict=True)))
        except ValueError:
            return None
    return found[0] if len(found) == 1 else None


def docker_networks() -> Networks:
    """Bridge-driver networks: name -> interface and subnet, plus the ones the chains can't cover."""
    try:
        ids = run([DOCKER, "network", "ls", "-q", "--no-trunc", "--filter", "driver=bridge"]).stdout.split()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise DockerUnavailable(type(e).__name__) from None
    if not ids:
        return Networks()
    try:
        # check=False: a network removed between `ls` and `inspect` makes inspect
        # exit 1 but still print the others.
        res = run([DOCKER, "network", "inspect", *ids], check=False)
        nets = json.loads(res.stdout or "[]")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        raise DockerUnavailable(type(e).__name__) from None
    if not isinstance(nets, list):
        raise DockerUnavailable("unexpected inspect output")
    if res.returncode != 0:
        # A network removed between `ls` and `inspect` is fine (inspect still prints
        # the others). dockerd going away is not: never render from an empty answer,
        # or the chain would be reloaded without its allow lines.
        if not nets:
            raise DockerUnavailable("network inspect returned nothing")
        try:
            run([DOCKER, "network", "ls", "-q", "--no-trunc", "--filter", "driver=bridge"])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            raise DockerUnavailable(type(e).__name__) from None
    out = Networks()
    for net in nets:
        name = str(net.get("Name", ""))
        iface = (net.get("Options") or {}).get("com.docker.network.bridge.name") or "br-" + str(net.get("Id", ""))[:12]
        if not _IFACE_RE.fullmatch(iface):
            out.uncovered.append(name)
            continue
        out.bridges[name] = iface
        covered = iface == "docker0" or iface.startswith("br-")  # the jumps: -i docker0, -i br-+
        subnet = _one_subnet(net)
        if subnet is not None:
            out.subnets[name] = subnet
        if not covered or net.get("EnableIPv6") or subnet is None:
            out.uncovered.append(name)
    # Other drivers (macvlan, ipvlan, ...) put containers on the LAN without a
    # bridge: nothing here sees them.
    try:
        drivers = run([DOCKER, "network", "ls", "--format", "{{.Driver}} {{.Name}}"]).stdout.splitlines()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise DockerUnavailable(type(e).__name__) from None
    for line in drivers:
        driver, _, name = line.strip().partition(" ")
        if driver and driver not in OK_DRIVERS and name not in out.uncovered:
            out.uncovered.append(name)
    return out


# --------------------------------------------------------------------------- rules


def _finish(fam: Hook, mode: str, bodies: dict[str, list[str]]) -> Ruleset:
    """bodies: chain -> its rules, for every chain of the hook (main chain without its head)."""
    parts = [fam.name, fam.builtin, mode]
    for chain in fam.chains:
        parts += [f":{chain}", *bodies[chain]]
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:12]
    head = (
        f"-A {fam.chain} -m conntrack --ctstate RELATED,ESTABLISHED --ctdir REPLY "
        f'-m comment --comment "jess-docker-input {digest}" -j RETURN'
    )
    chains = {fam.chain: [head, *bodies[fam.chain]], **{c: bodies[c] for c in fam.subchains}}
    lines = ["*filter", *(f":{c} - [0:0]" for c in fam.chains)]
    for c in fam.chains:
        lines += chains[c]
    text = "\n".join([*lines, "COMMIT", ""])
    counts = tuple((c, len(chains[c])) for c in fam.chains)
    return Ruleset(fam, text, digest, sum(n for _, n in counts), counts)


def _log_rule(chain: str, tag: str, hashname: str, mode: str, comment: str) -> str:
    # Per flow (source, destination, port) at most 2/min, and 30/min overall: one
    # chatty container can't hide the rare flows observe mode exists to find.
    kind = "observe" if mode == "observe" else "reject"
    return (
        f"-A {chain} -m pkttype --pkt-type unicast "
        "-m hashlimit --hashlimit-upto 2/min --hashlimit-burst 3 --hashlimit-mode srcip,dstip,dstport "
        f"--hashlimit-name {hashname} --hashlimit-htable-max 4096 "
        f'-m limit --limit 30/min --limit-burst 60 -m comment --comment "{comment}" '
        f'-j LOG --log-prefix "{tag} {kind}: " --log-level 6'
    )


def _source_rules(chain: str, spoof_chain: str, bridges: dict[str, str], subnets: dict[str, str]) -> list[str]:
    """`-i <bridge> ! -s <its subnet> -j <spoof chain>`, one per network with a known subnet."""
    rules = []
    for name in sorted(bridges):
        subnet = subnets.get(name)
        if subnet is None:
            continue
        rules.append(
            f'-A {chain} -i {bridges[name]} ! -s {subnet} -m comment --comment "jess: source" -j {spoof_chain}'
        )
    return rules


def _spoof_chain(chain: str, hashname: str, mode: str) -> list[str]:
    rules = [
        f'-A {chain} -m comment --comment "{SPOOFED_COMMENT}"',
        _log_rule(chain, SPOOF_TAG, hashname, mode, SPOOF_LOGGED_COMMENT),
    ]
    if mode == "enforce":
        rules.append(f"-A {chain} -j DROP")
    return rules


def _render_input(cfg: Config, bridges: dict[str, str], subnets: dict[str, str]) -> tuple[Ruleset, Ruleset]:
    enforce = cfg.mode == "enforce"
    quiet = "DROP" if enforce else "RETURN"
    chain = IPV4.chain
    v4 = _source_rules(chain, IN_SPOOF, bridges, subnets)
    v4.append(f'-A {chain} -m conntrack --ctstate INVALID -m comment --comment "jess: invalid" -j {quiet}')
    for a in cfg.allows:
        iface = bridges.get(a.network)
        if iface is None:
            continue
        ports = ",".join(str(p) for p in a.ports)
        v4.append(
            f"-A {chain} -i {iface} -p {a.proto} -m multiport --dports {ports} "
            f'-m comment --comment "jess allow {a.network}" -j RETURN'
        )
    v4 += [
        f'-A {chain} -m pkttype ! --pkt-type unicast -m comment --comment "jess: not unicast" -j {quiet}',
        f'-A {chain} -m comment --comment "{UNMATCHED_COMMENT}"',
        _log_rule(chain, IPV4.log_tag, IPV4.log_tag, cfg.mode, LOGGED_COMMENT),
    ]
    if enforce:
        v4 += [f"-A {chain} -p tcp -j REJECT --reject-with tcp-reset", f"-A {chain} -j DROP"]
    v6 = [
        f'-A {IPV6.chain} -m pkttype --pkt-type unicast -m comment --comment "{UNMATCHED_COMMENT}"',
        _log_rule(IPV6.chain, IPV6.log_tag, IPV6.log_tag, cfg.mode, LOGGED_COMMENT),
    ]
    if enforce:
        v6.append(f"-A {IPV6.chain} -j DROP")
    spoof = _spoof_chain(IN_SPOOF, "jess-spoof-in", cfg.mode)
    return _finish(IPV4, cfg.mode, {chain: v4, IN_SPOOF: spoof}), _finish(IPV6, cfg.mode, {IPV6.chain: v6})


def _render_forward(cfg: Config, bridges: dict[str, str], subnets: dict[str, str]) -> tuple[Ruleset, Ruleset]:
    mode = cfg.forward_mode
    enforce = mode == "enforce"
    fwd = FWD4.chain
    main = _source_rules(fwd, FWD_SPOOF, bridges, subnets)
    main.append(f'-A {fwd} -m conntrack --ctstate DNAT -m comment --comment "jess: published port" -j {FWD_LAN}')
    # Not DNAT'd and to another Docker bridge: Docker's own isolation rules decide.
    main += [f"-A {fwd} -o br-+ -j RETURN", f"-A {fwd} -o docker0 -j RETURN"]
    main += [f"-A {fwd} -d {net} -j {FWD_LAN}" for net in PRIVATE_V4]
    # Anything else is the internet: back to FORWARD and Docker's rules.
    quiet = "DROP" if enforce else "RETURN"
    lan = [f'-A {FWD_LAN} -m conntrack --ctstate INVALID -m comment --comment "jess: fwd invalid" -j {quiet}']
    for f in cfg.forwards:
        iface = bridges.get(f.network)
        if iface is None:
            continue
        for port in f.ports:
            lan.append(
                f"-A {FWD_LAN} -i {iface} -p {f.proto} -m conntrack --ctorigdst {f.dest} --ctorigdstport {port} "
                f'-m comment --comment "jess forward {f.network}" -j RETURN'
            )
    lan += [
        f'-A {FWD_LAN} -m comment --comment "{FWD_UNMATCHED_COMMENT}"',
        _log_rule(FWD_LAN, FWD4.log_tag, FWD4.log_tag, mode, FWD_LOGGED_COMMENT),
    ]
    if enforce:
        lan.append(f"-A {FWD_LAN} -j REJECT --reject-with icmp-admin-prohibited")
    spoof = _spoof_chain(FWD_SPOOF, "jess-spoof-fwd", mode)
    v6 = [
        f'-A {FWD6.chain} -m comment --comment "{FWD_UNMATCHED_COMMENT}"',
        _log_rule(FWD6.chain, FWD6.log_tag, FWD6.hashname, mode, FWD_LOGGED_COMMENT),
    ]
    if enforce:
        v6.append(f"-A {FWD6.chain} -j DROP")
    return _finish(FWD4, mode, {fwd: main, FWD_LAN: lan, FWD_SPOOF: spoof}), _finish(FWD6, mode, {FWD6.chain: v6})


def render(cfg: Config, bridges: dict[str, str], subnets: dict[str, str] | None = None) -> Rendered:
    """The *-restore texts that refill our chains (nothing else). No subnets = no source check."""
    subnets = subnets or {}
    rulesets = list(_render_input(cfg, bridges, subnets))
    wanted = [a.network for a in cfg.allows]
    absent: tuple[Hook, ...] = FORWARD_HOOKS
    if cfg.forward_mode != "off":
        rulesets += _render_forward(cfg, bridges, subnets)
        wanted += [f.network for f in cfg.forwards]
        absent = ()
    missing = tuple(dict.fromkeys(n for n in wanted if n not in bridges))
    return Rendered(tuple(rulesets), missing, absent)


def _jump_spec(fam: Hook, flag: str, iface: str) -> list[str]:
    return [flag, iface, "-m", "comment", "--comment", JUMP_COMMENT, "-j", fam.chain]


def _has_jump(fam: Hook, flag: str, iface: str) -> bool:
    return run([fam.iptables, "-w", "-C", fam.builtin, *_jump_spec(fam, flag, iface)], check=False).returncode == 0


def _jumps_on_top(fam: Hook) -> bool:
    """Our two jumps are the built-in chain's rules 1 and 2 (in either order)."""
    res = run([fam.iptables, "-w", "-S", fam.builtin], check=False)
    top = [line for line in res.stdout.splitlines() if line.startswith(f"-A {fam.builtin} ")][:2]
    ours = f"--comment {JUMP_COMMENT} -j {fam.chain}"
    return len(top) == 2 and top[0] != top[1] and all(line.endswith(ours) for line in top)


def live_rules(fam: Hook, chain: str | None = None) -> list[str] | None:
    """A chain's rules as `iptables -S` prints them, or None if it doesn't exist."""
    chain = chain or fam.chain
    live = run([fam.iptables, "-w", "-S", chain], check=False)
    if live.returncode != 0:
        return None
    return [line for line in live.stdout.splitlines() if line.startswith(f"-A {chain} ")]


def chain_in_sync(rs: Ruleset, loaded: dict[str, list[str] | None] | None = None) -> bool:
    """The live chains are this ruleset: per-chain rule counts + the digest, and when
    `loaded` (the chains as read back right after our last load) is given, every
    rule unchanged — which also catches a rule replaced in place (`iptables -R`)."""
    fam = rs.family
    for chain, count in rs.counts or ((fam.chain, rs.rule_count),):
        rules = live_rules(fam, chain)
        if rules is None or len(rules) != count:
            return False
        if chain == fam.chain and _DIGEST_RE.findall("\n".join(rules)) != [rs.digest]:
            return False
        if loaded is not None and loaded.get(chain) != rules:
            return False
    return True


def _delete_jumps(fam: Hook) -> None:
    for flag, iface in JUMPS:
        while _has_jump(fam, flag, iface):
            run([fam.iptables, "-w", "-D", fam.builtin, *_jump_spec(fam, flag, iface)])


def load_chain(rs: Ruleset) -> None:
    # --noflush is load-bearing: without it *-restore replaces the whole filter
    # table, Docker's chains included. The `:CHAIN` lines refill only ours.
    run([rs.family.restore, "-w", "--noflush"], input_text=rs.text)


def ensure_jumps(fam: Hook) -> bool:
    """Put our two jumps at positions 1-2 of the built-in chain; True if anything changed."""
    if _jumps_on_top(fam):
        return False
    _delete_jumps(fam)
    for flag, iface in JUMPS:  # each inserted at 1: ends as br-+, docker0, rest
        run([fam.iptables, "-w", "-I", fam.builtin, "1", *_jump_spec(fam, flag, iface)])
    return True


def apply_ruleset(rs: Ruleset) -> None:
    load_chain(rs)
    ensure_jumps(rs.family)


def _chain_exists(fam: Hook, chain: str) -> bool:
    return run([fam.iptables, "-w", "-S", chain], check=False).returncode == 0


def installed(fam: Hook) -> bool:
    """Any of the hook's jumps or chains is present."""
    return any(_chain_exists(fam, c) for c in fam.chains) or any(_has_jump(fam, f, i) for f, i in JUMPS)


def remove(fam: Hook) -> None:
    _delete_jumps(fam)
    present = [c for c in fam.chains if _chain_exists(fam, c)]
    for chain in present:  # flush all first: the main chain jumps to the subchains
        run([fam.iptables, "-w", "-F", chain])
    for chain in present:
        run([fam.iptables, "-w", "-X", chain])


def chain_counters(fam: Hook, chain: str | None = None) -> dict[str, int]:
    """Packet counters of a chain's commented rules, by comment."""
    res = run([fam.iptables, "-w", "-L", chain or fam.chain, "-n", "-v", "-x"], check=False)
    out: dict[str, int] = {}
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        m = re.search(r"/\* (.+?) \*/", line)
        if m:
            with contextlib.suppress(IndexError, ValueError):
                out.setdefault(m.group(1), int(line.split()[0]))
    return out


# --------------------------------------------------------------------------- metrics

Sample = tuple[str, dict[str, str], float]


def format_metrics(samples: list[Sample]) -> str:
    by_name: dict[str, list[Sample]] = collections.defaultdict(list)
    for s in samples:
        by_name[s[0]].append(s)
    lines: list[str] = []
    for name, (mtype, help_text) in METRICS.items():
        if name not in by_name:
            continue
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]
        for _, labels, value in by_name[name]:
            label_s = "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}" if labels else ""
            num = str(value) if isinstance(value, int) else f"{value:.3f}"
            lines.append(f"{name}{label_s} {num}")
    return "\n".join(lines) + "\n"


def write_metrics(metrics_dir: Path | None, text: str) -> None:
    if metrics_dir is None:
        return
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=metrics_dir, prefix=f".{METRICS_FILE}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(text)
        os.replace(tmp, metrics_dir / METRICS_FILE)
        tmp = None
    except OSError as e:
        log(f"cannot write metrics: {e.strerror}")
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


# --------------------------------------------------------------------------- watch


def _summary_line(cfg: Config, bridges: dict[str, str]) -> str:
    allow_rules = sum(1 for a in cfg.allows if a.network in bridges)
    line = f"mode={cfg.mode} allow_rules={allow_rules}"
    if cfg.forward_mode != "off":
        forward_rules = sum(1 for f in cfg.forwards if f.network in bridges)
        line += f" forward-mode={cfg.forward_mode} forward_rules={forward_rules}"
    return line


class Watcher:
    def __init__(self, config_path: Path, metrics_dir: Path | None):
        self.config_path = config_path
        self.metrics_dir = metrics_dir
        self.sha256 = script_sha256()
        self.started = time.time()
        self.last_apply = 0.0
        self.last_ok = False
        self.enforcing = 0
        self.forward_active = 0
        self.forward_enforcing = 0
        self.missing = 0
        self.uncovered: tuple[str, ...] = ()
        self.last_problem = ""
        self.loaded: dict[str, dict[str, list[str] | None]] = {}  # hook label -> chains as read back after our load

    def _problem(self, msg: str) -> None:
        self.last_ok = False
        if msg != self.last_problem:  # log state changes, not every retry
            log(msg)
            self.last_problem = msg

    def sync(self, *, force: bool = False) -> None:
        """Render from the current config + networks; apply each hook when forced or drifted."""
        try:
            cfg = load_config(self.config_path)
            nets = docker_networks()
            rendered = render(cfg, nets.bridges, nets.subnets)
            reloaded: list[str] = []
            for rs in rendered.rulesets:
                key = rs.family.label
                # A displaced or missing jump is fixed alone, without reloading the
                # chain (a reload resets its counters).
                if force or not chain_in_sync(rs, self.loaded.get(key)):
                    load_chain(rs)
                    self.loaded[key] = {c: live_rules(rs.family, c) for c in rs.family.chains}
                    reloaded.append(key)
                if ensure_jumps(rs.family):
                    log(f"{key}: jumps placed at the top of {rs.family.builtin}")
            for hook in rendered.absent:
                if installed(hook):
                    remove(hook)
                    self.loaded.pop(hook.label, None)
                    log(f"{hook.label}: chains and jumps removed (forward-mode off)")
            if reloaded:
                self.last_apply = time.time()
                log(
                    f"applied {'+'.join(reloaded)} {_summary_line(cfg, nets.bridges)}"
                    + (f" missing_networks={','.join(rendered.missing)}" if rendered.missing else "")
                )
            if tuple(nets.uncovered) != self.uncovered:
                self.uncovered = tuple(nets.uncovered)
                if self.uncovered:
                    log(f"networks the chains do not cover: {','.join(self.uncovered)}")
            self.enforcing = int(cfg.mode == "enforce")
            self.forward_active = int(cfg.forward_mode != "off")
            self.forward_enforcing = int(cfg.forward_mode == "enforce")
            self.missing = len(rendered.missing)
            self.last_ok = True
            if self.last_problem:
                log("recovered")
                self.last_problem = ""
        except ConfigError as e:
            self._problem(f"config error, chains left as they are: {e}")
        except DockerUnavailable as e:
            self._problem(f"docker unavailable ({e}), chains left as they are")
        except (subprocess.SubprocessError, OSError) as e:
            self._problem(f"iptables failed ({type(e).__name__}), chains left as they are, will retry")
        self.metrics()

    def metrics(self) -> None:
        try:
            samples: list[Sample] = [
                ("jess_docker_input_info", {"sha256": self.sha256[:12]}, 1),
                ("jess_docker_input_started_timestamp_seconds", {}, self.started),
                ("jess_docker_input_last_check_timestamp_seconds", {}, time.time()),
                ("jess_docker_input_last_apply_timestamp_seconds", {}, self.last_apply),
                ("jess_docker_input_check_ok", {}, int(self.last_ok)),
                ("jess_docker_input_enforcing", {}, self.enforcing),
                ("jess_docker_input_forward_active", {}, self.forward_active),
                ("jess_docker_input_forward_enforcing", {}, self.forward_enforcing),
                ("jess_docker_input_networks_missing", {}, self.missing),
                ("jess_docker_input_networks_uncovered", {}, len(self.uncovered)),
            ]
            counted = [
                (fam, fam.chain, UNMATCHED_COMMENT, "jess_docker_input_unmatched_packets_total", {"family": fam.name})
                for fam in FAMILIES
            ]
            counted += [
                (fam, fam.chain, LOGGED_COMMENT, "jess_docker_input_logged_packets_total", {"family": fam.name})
                for fam in FAMILIES
            ]
            counted.append(
                (IPV4, IN_SPOOF, SPOOFED_COMMENT, "jess_docker_input_spoofed_packets_total", {"path": "input"})
            )
            if self.forward_active:
                for fam, chain in ((FWD4, FWD_LAN), (FWD6, FWD6.chain)):
                    lab = {"family": fam.name}
                    counted.append(
                        (fam, chain, FWD_UNMATCHED_COMMENT, "jess_docker_input_forward_unmatched_packets_total", lab)
                    )
                    counted.append(
                        (fam, chain, FWD_LOGGED_COMMENT, "jess_docker_input_forward_logged_packets_total", lab)
                    )
                counted.append(
                    (FWD4, FWD_SPOOF, SPOOFED_COMMENT, "jess_docker_input_spoofed_packets_total", {"path": "forward"})
                )
            cache: dict[tuple[str, str], dict[str, int]] = {}
            for fam, chain, comment, metric, labels in counted:
                key = (fam.iptables, chain)
                if key not in cache:
                    cache[key] = chain_counters(fam, chain)
                if comment in cache[key]:
                    samples.append((metric, labels, cache[key][comment]))
            write_metrics(self.metrics_dir, format_metrics(samples))
        except (subprocess.SubprocessError, OSError) as e:
            log(f"metrics skipped ({type(e).__name__})")

    def run_forever(self) -> None:
        wake_r, wake_w = os.pipe()
        os.set_blocking(wake_w, False)
        signal.set_wakeup_fd(wake_w)
        reload_requested = False

        def on_hup(_sig, _frm):
            nonlocal reload_requested
            reload_requested = True

        signal.signal(signal.SIGHUP, on_hup)
        backoff = 5.0
        while True:
            # Start the event stream BEFORE the sync, so a network created in
            # between still triggers one.
            stream_started = time.monotonic()
            events = subprocess.Popen(
                [DOCKER, "events", "--filter", "type=network", "--filter", "event=create",
                 "--filter", "event=destroy", "--format", "{{.Action}}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )  # fmt: skip
            assert events.stdout is not None
            self.sync(force=not self.last_ok)
            next_check = time.monotonic() + CHECK_INTERVAL_S
            while True:
                timeout = max(0.0, next_check - time.monotonic())
                ready, _, _ = select.select([events.stdout, wake_r], [], [], timeout)
                synced = False
                if wake_r in ready:
                    os.read(wake_r, 512)
                if reload_requested:
                    reload_requested = False
                    log("SIGHUP: re-reading config")
                    self.sync(force=True)
                    synced = True
                if events.stdout in ready:
                    if not events.stdout.readline():
                        break  # dockerd went away; restart the stream
                    time.sleep(1.0)  # let a burst of network events settle
                    self.sync()  # re-applies only if an allow-listed bridge changed
                    synced = True
                if not synced and time.monotonic() >= next_check:
                    self.sync()
                    synced = True
                if synced:
                    next_check = time.monotonic() + CHECK_INTERVAL_S
            events.stdout.close()
            try:
                events.wait(timeout=10)
            except subprocess.TimeoutExpired:
                events.kill()
                events.wait()
            backoff = 5.0 if time.monotonic() - stream_started > 120 else min(backoff * 2, 60.0)
            self._problem("docker events stream ended, retrying")
            time.sleep(backoff)


# --------------------------------------------------------------------------- summary


def summary(hours: int) -> str:
    res = run([JOURNALCTL, "-k", "--no-pager", "-o", "cat", f"--since=-{hours}h", "-g", "jess-docker-"], check=False)
    try:
        by_iface = {iface: name for name, iface in docker_networks().bridges.items()}
    except DockerUnavailable:
        by_iface = {}
    counts: collections.Counter = collections.Counter()
    for line in res.stdout.splitlines():
        m = _KLOG_RE.search(line)
        if m:
            tag, kind, iface, dst, proto, dpt = m.groups()
            counts[(_KLOG_PATH[tag], kind, by_iface.get(iface, iface), dst, proto, dpt or "-")] += 1
    if not counts:
        return f"no jess-docker log lines in the last {hours} h"
    header = f"{'count':6}  {'path':6}  {'kind':7}  {'from network':36}  {'to':25}  {'proto':6}  port"
    rows = [
        f"{n:6}  {path:6}  {kind:7}  {net:36}  {dst:25}  {proto:6}  {dpt}"
        for (path, kind, net, dst, proto, dpt), n in counts.most_common()
    ]
    return "\n".join([header, *rows])


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("command", choices=("render", "check", "apply", "watch", "remove", "status", "summary"))
    ap.add_argument("--config", type=Path, default=Path("/etc/jess-docker-input.conf"))
    ap.add_argument("--metrics-dir", type=Path, help="node_exporter textfile collector directory (watch only)")
    ap.add_argument("--hours", type=int, default=24, help="summary: how far back to read the kernel log (default 24)")
    ap.add_argument(
        "--only-if-clean-stop",
        action="store_true",
        help="remove: do nothing unless systemd reports a clean stop (SERVICE_RESULT=success)",
    )
    args = ap.parse_args(argv)

    try:
        if args.command == "remove":
            result = os.environ.get("SERVICE_RESULT", "success")
            if args.only_if_clean_stop and result != "success":
                log(f"service result {result!r}: keeping the chains for the restart")
                return 0
            for fam in HOOKS:
                remove(fam)
            log("removed")
            return 0
        if args.command == "status":
            print(f"script sha256: {script_sha256()}")
            rc = 0
            for fam in HOOKS:
                for chain in fam.chains:
                    res = run([fam.iptables, "-w", "-L", chain, "-n", "-v", "--line-numbers"], check=False)
                    print(res.stdout or f"chain {chain} not present\n")
                    if fam.builtin == "INPUT" and chain == fam.chain:
                        rc = rc or res.returncode  # FORWARD chains are absent with forward-mode off
            return 0 if rc == 0 else 1
        if args.command == "summary":
            print(summary(args.hours))
            return 0
        if args.command == "watch":
            Watcher(args.config, args.metrics_dir).run_forever()
            return 0
        cfg = load_config(args.config)
        nets = docker_networks()
        rendered = render(cfg, nets.bridges, nets.subnets)
        if rendered.missing:
            log(f"allow/forward networks not found (no rule): {', '.join(rendered.missing)}")
        if args.command == "check":
            digests = " ".join(f"{rs.family.label}={rs.digest}" for rs in rendered.rulesets)
            log(f"config ok: {_summary_line(cfg, nets.bridges)} {digests}")
            return 0
        if args.command == "render":
            for rs in rendered.rulesets:
                print(f"# {rs.family.restore} --noflush", rs.text, sep="\n", end="")
            return 0
        for rs in rendered.rulesets:
            apply_ruleset(rs)
        for hook in rendered.absent:
            if installed(hook):
                remove(hook)
        log("applied")
        return 0
    except ConfigError as e:
        log(f"config error: {e}")
        return 2
    except DockerUnavailable as e:
        log(f"docker unavailable: {e}")
        return 3
    except subprocess.CalledProcessError as e:
        log(f"{e.cmd[0]} failed (exit {e.returncode}): {(e.stderr or '').strip()[:300]}")
        return 1
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"failed: {type(e).__name__}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
