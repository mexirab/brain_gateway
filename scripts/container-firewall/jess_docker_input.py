#!/usr/bin/env python3
"""Limit which Jupiter host ports Docker containers can reach (host INPUT allow-list).

Why this exists
---------------
Measured 2026-11-21 (docs/internal/CONTAINER_HOST_REACHABILITY.md): every
container on an ordinary Docker network could open connections to every Jupiter
service listening on all interfaces (SSH, SMB, Pi-hole, Home Assistant, Netdata,
node-exporter, ...), through any host address. ufw is inactive, and Docker's
`isolated` gateway mode is refused for networks with internet access. So the fix
is on the host: packets that arrive from a Docker bridge and are addressed to
the host itself go through one chain per IP family, which lets the needed
(network, port) pairs through and logs (observe) or rejects (enforce) the rest.

What it does
------------
- Chains JESS-DOCKER-IN (iptables) and JESS-DOCKER-IN6 (ip6tables), jumped from
  the top of INPUT for `-i br-+` and `-i docker0` ONLY. Loopback, LAN and
  tailnet traffic never enters them, so they cannot lock out SSH. Packets that
  pass `RETURN` to INPUT (policy ACCEPT, `ts-input` still runs). The jumps are
  kept at INPUT positions 1-2 (tailscaled re-inserts its jump at the top when it
  restarts).
- IPv4 rule order: replies to connections the HOST opened (`--ctdir REPLY`) pass;
  conntrack INVALID and non-unicast packets are dropped silently (passed in
  observe mode); then the `allow` lines, checked on every packet of a
  container-opened connection (so a connection opened during observe, or to a
  port later removed from the list, is cut in enforce); then the rest is
  counted, logged (per-flow + global rate limit, kernel header fields only) and,
  in enforce mode, rejected (TCP reset) or dropped.
- IPv6: no Docker network here has IPv6, but every bridge has a host link-local
  address and the host's services listen on `[::]`, so a container with
  NET_RAW (in Docker's default capability set) could reach them with hand-made
  packets. JESS-DOCKER-IN6 has no allow lines: host replies pass, the rest is
  counted, logged and (enforce) dropped.
- `allow <network> <tcp|udp> <ports>` lines name Docker networks. The bridge
  interface of each is looked up at apply time (the network's
  `com.docker.network.bridge.name`, else `br-<first 12 hex of the id>`). A
  network that doesn't exist (yet) gets no allow rule: it fails closed in
  enforce mode until it exists and the next sync runs.
- A packet arrives on the bridge of the network the container's DEFAULT ROUTE
  uses, not necessarily the network named in an allow line. That is why the
  allow-listed networks are dedicated ones that carry the default route of
  their (only) members via compose `gw_priority`: host-access (orchestrator)
  and host-scrape (Prometheus, blackbox-exporter).
- Each chain is refilled in one `*-restore --noflush` transaction (never without
  --noflush: that would wipe Docker's own rules). The first rule carries a hash
  of the rendered ruleset; drift (a flushed or edited chain, a removed or
  displaced jump, a recreated network) is detected and repaired every 60 s
  without touching a healthy chain.

Commands
--------
  render   print the rulesets for the current config + Docker networks (no changes)
  check    validate the config and reach dockerd; one line, exit 0/2/3 (unit ExecReload)
  apply    apply once
  watch    sync, then again on Docker network create/destroy events, on SIGHUP
           (re-reads the config) and every 60 s (drift check). Used by the unit.
  remove   delete the INPUT jumps and both chains (= no filtering). With
           --only-if-clean-stop (unit ExecStopPost) it does nothing after a crash,
           so a crash-restart keeps the rules in place.
  status   print the script's sha256 and both chains with packet counters
  summary  group the chains' kernel log lines (last --hours, default 24) by
           network, address, port

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
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DOCKER = "/usr/bin/docker"
JOURNALCTL = "/usr/bin/journalctl"
JUMP_COMMENT = "jess-docker-input"
JUMPS = (("-i", "docker0"), ("-i", "br-+"))
UNMATCHED_COMMENT = "jess: unmatched"
LOGGED_COMMENT = "jess: logged"
MODES = ("observe", "enforce")
PROTOS = ("tcp", "udp")
MAX_PORTS_PER_RULE = 15  # iptables multiport limit
CHECK_INTERVAL_S = 60.0
METRICS_FILE = "jess_docker_input.prom"
_NETWORK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_IFACE_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,14}")
_DIGEST_RE = re.compile(r'jess-docker-input ([0-9a-f]{12})"')
_KLOG_RE = re.compile(r"(jess-docker-in6?) (observe|reject): .*?IN=(\S+) .*?DST=(\S+) .*?PROTO=(\S+)(?: .*?DPT=(\d+))?")


@dataclass(frozen=True)
class Family:
    name: str
    iptables: str
    restore: str
    chain: str
    log_tag: str


IPV4 = Family("ipv4", "/usr/sbin/iptables", "/usr/sbin/iptables-restore", "JESS-DOCKER-IN", "jess-docker-in")
IPV6 = Family("ipv6", "/usr/sbin/ip6tables", "/usr/sbin/ip6tables-restore", "JESS-DOCKER-IN6", "jess-docker-in6")
FAMILIES = (IPV4, IPV6)

METRICS = {  # name: (type, help)
    "jess_docker_input_info": ("gauge", "Always 1; the sha256 label identifies the installed script."),
    "jess_docker_input_started_timestamp_seconds": ("gauge", "When the watcher process started."),
    "jess_docker_input_last_check_timestamp_seconds": ("gauge", "Last sync attempt (heartbeat, every 60 s)."),
    "jess_docker_input_last_apply_timestamp_seconds": ("gauge", "Last time a chain was (re)loaded."),
    "jess_docker_input_check_ok": ("gauge", "1 = last sync succeeded; 0 = config error, dockerd or iptables failed."),
    "jess_docker_input_enforcing": ("gauge", "1 = enforce mode, 0 = observe mode (as of the last good sync)."),
    "jess_docker_input_networks_missing": ("gauge", "Allow-listed Docker networks that do not exist."),
    "jess_docker_input_networks_uncovered": ("gauge", "Bridge networks the jumps do not cover (name or IPv6)."),
    "jess_docker_input_unmatched_packets_total": (
        "counter",
        "Unicast packets from containers to the host that no rule allowed (resets when a chain is reloaded).",
    ),
    "jess_docker_input_logged_packets_total": ("counter", "Unmatched packets that were also logged (rate-limited)."),
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
class Config:
    mode: str
    allows: tuple[Allow, ...]


@dataclass
class Networks:
    bridges: dict[str, str] = field(default_factory=dict)  # network name -> bridge interface
    uncovered: list[str] = field(default_factory=list)  # bridge networks the jumps can't see


@dataclass(frozen=True)
class Ruleset:
    family: Family
    text: str
    digest: str
    rule_count: int


@dataclass(frozen=True)
class Rendered:
    rulesets: tuple[Ruleset, ...]
    missing: tuple[str, ...]


def log(msg: str) -> None:
    print(f"jess-docker-input: {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, check=check, timeout=60)


def script_sha256() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return "unknown"


# --------------------------------------------------------------------------- config


def parse_config(text: str) -> Config:
    mode: str | None = None
    allows: list[Allow] = []
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
        elif parts[0] == "allow":
            if len(parts) != 4:
                raise ConfigError(f"line {lineno}: expected 'allow <network> <tcp|udp> <port>[,<port>...]'")
            _, network, proto, ports_s = parts
            if not _NETWORK_RE.fullmatch(network):
                raise ConfigError(f"line {lineno}: bad network name")
            if proto not in PROTOS:
                raise ConfigError(f"line {lineno}: protocol must be tcp or udp")
            if not re.fullmatch(r"[0-9]{1,5}(,[0-9]{1,5})*", ports_s):
                raise ConfigError(f"line {lineno}: ports must be numbers separated by commas")
            ports = tuple(sorted({int(p) for p in ports_s.split(",")}))
            if any(not 1 <= p <= 65535 for p in ports):
                raise ConfigError(f"line {lineno}: ports must be 1-65535")
            if len(ports) > MAX_PORTS_PER_RULE:
                raise ConfigError(f"line {lineno}: at most {MAX_PORTS_PER_RULE} ports per line")
            allows.append(Allow(network, proto, ports))
        else:
            raise ConfigError(f"line {lineno}: unknown keyword {parts[0]!r}")
    if mode is None:
        raise ConfigError("no 'mode' line")
    return Config(mode, tuple(allows))


def load_config(path: Path) -> Config:
    try:
        return parse_config(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None


# --------------------------------------------------------------------------- docker


def docker_networks() -> Networks:
    """Bridge-driver networks: name -> interface, plus the ones the jumps can't cover."""
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
        if not covered or net.get("EnableIPv6"):
            out.uncovered.append(name)
    return out


# --------------------------------------------------------------------------- rules


def _finish(fam: Family, mode: str, body: list[str]) -> Ruleset:
    digest = hashlib.sha256("\n".join([fam.name, mode, *body]).encode()).hexdigest()[:12]
    head = (
        f"-A {fam.chain} -m conntrack --ctstate RELATED,ESTABLISHED --ctdir REPLY "
        f'-m comment --comment "jess-docker-input {digest}" -j RETURN'
    )
    text = "\n".join(["*filter", f":{fam.chain} - [0:0]", head, *body, "COMMIT", ""])
    return Ruleset(fam, text, digest, 1 + len(body))


def _log_rule(fam: Family, mode: str) -> str:
    # Per flow (source, destination, port) at most 2/min, and 30/min overall: one
    # chatty container can't hide the rare flows observe mode exists to find.
    kind = "observe" if mode == "observe" else "reject"
    return (
        f"-A {fam.chain} -m pkttype --pkt-type unicast "
        "-m hashlimit --hashlimit-upto 2/min --hashlimit-burst 3 --hashlimit-mode srcip,dstip,dstport "
        f"--hashlimit-name {fam.log_tag} --hashlimit-htable-max 4096 "
        f'-m limit --limit 30/min --limit-burst 60 -m comment --comment "{LOGGED_COMMENT}" '
        f'-j LOG --log-prefix "{fam.log_tag} {kind}: " --log-level 6'
    )


def render(cfg: Config, bridges: dict[str, str]) -> Rendered:
    """The *-restore texts that refill JESS-DOCKER-IN and JESS-DOCKER-IN6 (nothing else)."""
    enforce = cfg.mode == "enforce"
    quiet = "DROP" if enforce else "RETURN"
    chain = IPV4.chain
    v4: list[str] = [f'-A {chain} -m conntrack --ctstate INVALID -m comment --comment "jess: invalid" -j {quiet}']
    missing: list[str] = []
    for a in cfg.allows:
        iface = bridges.get(a.network)
        if iface is None:
            missing.append(a.network)
            continue
        ports = ",".join(str(p) for p in a.ports)
        v4.append(
            f"-A {chain} -i {iface} -p {a.proto} -m multiport --dports {ports} "
            f'-m comment --comment "jess allow {a.network}" -j RETURN'
        )
    v4 += [
        f'-A {chain} -m pkttype ! --pkt-type unicast -m comment --comment "jess: not unicast" -j {quiet}',
        f'-A {chain} -m comment --comment "{UNMATCHED_COMMENT}"',
        _log_rule(IPV4, cfg.mode),
    ]
    if enforce:
        v4 += [f"-A {chain} -p tcp -j REJECT --reject-with tcp-reset", f"-A {chain} -j DROP"]
    v6 = [
        f'-A {IPV6.chain} -m pkttype --pkt-type unicast -m comment --comment "{UNMATCHED_COMMENT}"',
        _log_rule(IPV6, cfg.mode),
    ]
    if enforce:
        v6.append(f"-A {IPV6.chain} -j DROP")
    return Rendered((_finish(IPV4, cfg.mode, v4), _finish(IPV6, cfg.mode, v6)), tuple(missing))


def _jump_spec(fam: Family, flag: str, iface: str) -> list[str]:
    return [flag, iface, "-m", "comment", "--comment", JUMP_COMMENT, "-j", fam.chain]


def _has_jump(fam: Family, flag: str, iface: str) -> bool:
    return run([fam.iptables, "-w", "-C", "INPUT", *_jump_spec(fam, flag, iface)], check=False).returncode == 0


def _jumps_on_top(fam: Family) -> bool:
    """Our two jumps are INPUT rules 1 and 2 (in either order)."""
    res = run([fam.iptables, "-w", "-S", "INPUT"], check=False)
    top = [line for line in res.stdout.splitlines() if line.startswith("-A INPUT ")][:2]
    ours = f"--comment {JUMP_COMMENT} -j {fam.chain}"
    return len(top) == 2 and top[0] != top[1] and all(line.endswith(ours) for line in top)


def live_rules(fam: Family) -> list[str] | None:
    """The chain's rules as `iptables -S` prints them, or None if it doesn't exist."""
    live = run([fam.iptables, "-w", "-S", fam.chain], check=False)
    if live.returncode != 0:
        return None
    return [line for line in live.stdout.splitlines() if line.startswith(f"-A {fam.chain} ")]


def chain_in_sync(rs: Ruleset, loaded: list[str] | None = None) -> bool:
    """The live chain is this ruleset: digest + rule count, and when `loaded` (the
    chain as read back right after our last load) is given, every rule unchanged —
    which also catches a rule replaced in place (`iptables -R`)."""
    rules = live_rules(rs.family)
    if rules is None:
        return False
    if len(rules) != rs.rule_count or _DIGEST_RE.findall("\n".join(rules)) != [rs.digest]:
        return False
    return loaded is None or rules == loaded


def _delete_jumps(fam: Family) -> None:
    for flag, iface in JUMPS:
        while _has_jump(fam, flag, iface):
            run([fam.iptables, "-w", "-D", "INPUT", *_jump_spec(fam, flag, iface)])


def load_chain(rs: Ruleset) -> None:
    # --noflush is load-bearing: without it *-restore replaces the whole filter
    # table, Docker's chains included. The `:CHAIN` line refills only ours.
    run([rs.family.restore, "-w", "--noflush"], input_text=rs.text)


def ensure_jumps(fam: Family) -> bool:
    """Put our two jumps at INPUT positions 1-2; True if anything changed."""
    if _jumps_on_top(fam):
        return False
    _delete_jumps(fam)
    for flag, iface in JUMPS:  # each inserted at 1: ends as br-+, docker0, rest
        run([fam.iptables, "-w", "-I", "INPUT", "1", *_jump_spec(fam, flag, iface)])
    return True


def apply_ruleset(rs: Ruleset) -> None:
    load_chain(rs)
    ensure_jumps(rs.family)


def remove(fam: Family) -> None:
    _delete_jumps(fam)
    if run([fam.iptables, "-w", "-S", fam.chain], check=False).returncode == 0:
        run([fam.iptables, "-w", "-F", fam.chain])
        run([fam.iptables, "-w", "-X", fam.chain])


def chain_counters(fam: Family) -> dict[str, int]:
    """Packet counters of the commented counting rules, by comment."""
    res = run([fam.iptables, "-w", "-L", fam.chain, "-n", "-v", "-x"], check=False)
    out: dict[str, int] = {}
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        for comment in (UNMATCHED_COMMENT, LOGGED_COMMENT):
            if f"/* {comment} */" in line:
                with contextlib.suppress(IndexError, ValueError):
                    out[comment] = int(line.split()[0])
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


class Watcher:
    def __init__(self, config_path: Path, metrics_dir: Path | None):
        self.config_path = config_path
        self.metrics_dir = metrics_dir
        self.sha256 = script_sha256()
        self.started = time.time()
        self.last_apply = 0.0
        self.last_ok = False
        self.enforcing = 0
        self.missing = 0
        self.uncovered: tuple[str, ...] = ()
        self.last_problem = ""
        self.loaded: dict[str, list[str] | None] = {}  # family -> chain as read back after our load

    def _problem(self, msg: str) -> None:
        self.last_ok = False
        if msg != self.last_problem:  # log state changes, not every retry
            log(msg)
            self.last_problem = msg

    def sync(self, *, force: bool = False) -> None:
        """Render from the current config + networks; apply each chain when forced or drifted."""
        try:
            cfg = load_config(self.config_path)
            nets = docker_networks()
            rendered = render(cfg, nets.bridges)
            reloaded: list[str] = []
            for rs in rendered.rulesets:
                # A displaced or missing jump is fixed alone, without reloading the
                # chain (a reload resets its counters).
                if force or not chain_in_sync(rs, self.loaded.get(rs.family.name)):
                    load_chain(rs)
                    self.loaded[rs.family.name] = live_rules(rs.family)
                    reloaded.append(rs.family.name)
                if ensure_jumps(rs.family):
                    log(f"{rs.family.name}: jumps placed at the top of INPUT")
            if reloaded:
                self.last_apply = time.time()
                log(
                    f"applied {'+'.join(reloaded)} mode={cfg.mode} "
                    f"allow_rules={len(cfg.allows) - len(rendered.missing)}"
                    + (f" missing_networks={','.join(rendered.missing)}" if rendered.missing else "")
                )
            if tuple(nets.uncovered) != self.uncovered:
                self.uncovered = tuple(nets.uncovered)
                if self.uncovered:
                    log(f"bridge networks the jumps do not cover: {','.join(self.uncovered)}")
            self.enforcing = int(cfg.mode == "enforce")
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
                ("jess_docker_input_networks_missing", {}, self.missing),
                ("jess_docker_input_networks_uncovered", {}, len(self.uncovered)),
            ]
            for fam in FAMILIES:
                counters = chain_counters(fam)
                if UNMATCHED_COMMENT in counters:
                    samples.append(
                        ("jess_docker_input_unmatched_packets_total", {"family": fam.name}, counters[UNMATCHED_COMMENT])
                    )
                if LOGGED_COMMENT in counters:
                    samples.append(
                        ("jess_docker_input_logged_packets_total", {"family": fam.name}, counters[LOGGED_COMMENT])
                    )
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
    res = run([JOURNALCTL, "-k", "--no-pager", "-o", "cat", f"--since=-{hours}h", "-g", "jess-docker-in"], check=False)
    try:
        by_iface = {iface: name for name, iface in docker_networks().bridges.items()}
    except DockerUnavailable:
        by_iface = {}
    counts: collections.Counter = collections.Counter()
    for line in res.stdout.splitlines():
        m = _KLOG_RE.search(line)
        if m:
            tag, kind, iface, dst, proto, dpt = m.groups()
            family = "ipv6" if tag.endswith("6") else "ipv4"
            counts[(family, kind, by_iface.get(iface, iface), dst, proto, dpt or "-")] += 1
    if not counts:
        return f"no jess-docker-in log lines in the last {hours} h"
    header = f"{'count':6}  {'family':6}  {'kind':7}  {'from network':36}  {'to':25}  {'proto':6}  port"
    rows = [
        f"{n:6}  {fam:6}  {kind:7}  {net:36}  {dst:25}  {proto:6}  {dpt}"
        for (fam, kind, net, dst, proto, dpt), n in counts.most_common()
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
            for fam in FAMILIES:
                remove(fam)
            log("removed")
            return 0
        if args.command == "status":
            print(f"script sha256: {script_sha256()}")
            rc = 0
            for fam in FAMILIES:
                res = run([fam.iptables, "-w", "-L", fam.chain, "-n", "-v", "--line-numbers"], check=False)
                print(res.stdout or f"chain {fam.chain} not present\n")
                rc = rc or res.returncode
            return 0 if rc == 0 else 1
        if args.command == "summary":
            print(summary(args.hours))
            return 0
        if args.command == "watch":
            Watcher(args.config, args.metrics_dir).run_forever()
            return 0
        cfg = load_config(args.config)
        rendered = render(cfg, docker_networks().bridges)
        if rendered.missing:
            log(f"allow-list networks not found (no rule): {', '.join(rendered.missing)}")
        if args.command == "check":
            digests = " ".join(f"{rs.family.name}={rs.digest}" for rs in rendered.rulesets)
            log(f"config ok: mode={cfg.mode} allow_rules={len(cfg.allows) - len(rendered.missing)} {digests}")
            return 0
        if args.command == "render":
            for rs in rendered.rulesets:
                print(f"# {rs.family.restore} --noflush", rs.text, sep="\n", end="")
            return 0
        for rs in rendered.rulesets:
            apply_ruleset(rs)
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
