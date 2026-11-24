"""
Tests for scripts/container-firewall/jess_docker_input.py, the Jupiter host
INPUT allow-list for traffic from Docker bridges to the host itself
(docs/internal/CONTAINER_HOST_REACHABILITY.md).

No root, iptables or docker: the module's single `run()` seam is replaced by an
in-memory Host that models what the script uses of the real CLIs:

- docker `network ls -q --no-trunc --filter driver=bridge`,
  `network inspect <ids>` (exit 1 plus the networks it did find when one
  vanished in between, like the real CLI; IPAM subnets included) and
  `network ls --format '{{.Driver}} {{.Name}}'`,
- iptables / ip6tables `-w` with -S / -C / -I / -D / -F / -X / -L, per family,
  on INPUT and FORWARD (FORWARD starts as Docker + tailscaled leave it). Like the
  kernel: -I to a chain that doesn't exist fails, -X of a chain that is still
  referenced or not empty fails,
- `*-restore -w --noflush`: parsed strictly (*filter, `:CHAIN - [0:0]`, -A lines
  with balanced quotes, COMMIT + newline), applied atomically, counters reset.
  A restore WITHOUT --noflush would wipe Docker's own rules on the real host:
  the fake refuses it and the fixture fails the test,
- journalctl -k (for `summary`).

Any other command is recorded as unexpected and fails the test at teardown.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "container-firewall"
_SCRIPT = _DIR / "jess_docker_input.py"
_REPO_CONF = _DIR / "jess-docker-input.conf"


def _load():
    spec = importlib.util.spec_from_file_location("jess_docker_input_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Must be registered BEFORE exec: @dataclass resolves the string annotations
    # (from __future__ import annotations) via sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


jdi = _load()
IPV4, IPV6 = jdi.IPV4, jdi.IPV6
FAM = {"ipv4": IPV4, "ipv6": IPV6}
V4_CHAIN, V6_CHAIN = IPV4.chain, IPV6.chain

HOSTACC, SCRAPE = "gateway_nerves_host-access", "monitoring_host-scrape"
GWNET, MONNET = "gateway_nerves_gateway-net", "monitoring_monitoring-net"
BRIDGES = {HOSTACC: "br-jess-hostacc", SCRAPE: "br-jess-scrape"}
NO_CHAIN = "iptables: No chain/target/match by that name."
LOCKED = "Another app is currently holding the xtables lock."
BUILTIN_TARGETS = {"ACCEPT", "DROP", "RETURN", "REJECT", "LOG"}
TS_JUMP = ("-j", "ts-input")  # tailscaled's INPUT jump
TS_FWD = ("-j", "ts-forward")  # tailscaled's FORWARD jump
DOCKER_FWD = [("-j", "DOCKER-USER"), ("-j", "DOCKER-FORWARD"), TS_FWD]  # FORWARD as Docker 29 + tailscaled leave it
FOREIGN_CHAINS = ("ts-input", "ts-forward", "DOCKER-USER", "DOCKER-FORWARD")
FWD4, FWD6 = jdi.FWD4, jdi.FWD6


def _cfg(mode="observe", extra=""):
    return jdi.parse_config(
        f"mode {mode}\nallow {HOSTACC} tcp 8004,8053,8123,8888\nallow {SCRAPE} tcp 5050,9100,9400,19999\n{extra}"
    )


def _net_id(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _jump(fam, iface, hook=None):
    """One of our jumps as `iptables -S INPUT|FORWARD` lists it (without `-A INPUT`)."""
    return ("-i", iface, "-m", "comment", "--comment", "jess-docker-input", "-j", (hook or FAM[fam]).chain)


def _ours(fam, hook=None):
    return {_jump(fam, "br-+", hook), _jump(fam, "docker0", hook)}


def _subnet(name):
    h = hashlib.sha256(name.encode()).digest()
    return f"10.{h[0]}.{h[1]}.0/24"


def _target(rule):
    return rule[rule.index("-j") + 1] if "-j" in rule else None


def _q(tok):
    return f'"{tok}"' if not tok or any(c.isspace() for c in tok) else tok


def _rules(rs):
    return [line for line in rs.text.split("\n") if line.startswith("-A ")]


def _main(rs):
    """Only the hook's main chain (the one the jumps point at)."""
    return [r for r in _rules(rs) if r.startswith(f"-A {rs.family.chain} ")]


def _rule(rs, comment):
    (hit,) = [r for r in _rules(rs) if f'--comment "{comment}"' in r]
    return hit


# --------------------------------------------------------------------------
# Fake host
# --------------------------------------------------------------------------


class Host:
    def __init__(self):
        self.input = {f: [TS_JUMP] for f in FAM}  # INPUT rules as token tuples, in order
        self.forward = {f: list(DOCKER_FWD) for f in FAM}  # FORWARD rules, likewise
        self.chains = {f: {c: [] for c in FOREIGN_CHAINS} for f in FAM}  # user chain -> ["-A CHAIN ..."]
        self.counters = {f: {} for f in FAM}  # rule comment -> packet count
        self.networks: dict[str, dict] = {}  # id -> docker inspect dict
        self.journal = ""
        self.fail: dict[str, object] = {}  # "docker", "docker ls", "ipv4", "ipv4 -I", "ipv6 restore" ...
        self.vanish: set[str] = set()  # network ids removed between `ls` and `inspect`
        self.calls: list[tuple[str, ...]] = []
        self.restores: list[tuple[str, tuple[str, ...]]] = []  # (family, argv) of every attempt
        self.unexpected: list[tuple[str, ...]] = []

    # -- docker ----------------------------------------------------------
    def add_network(self, name, bridge_name=None, *, ipv6=False, driver="bridge", net_id=None, subnets=None):
        nid = net_id or _net_id(name)
        opts = {"com.docker.network.bridge.enable_icc": "true"}
        if bridge_name is not None:
            opts["com.docker.network.bridge.name"] = bridge_name
        subnets = [_subnet(name)] if subnets is None else subnets
        ipam = {"Driver": "default", "Config": [{"Subnet": s} for s in subnets]}
        self.networks[nid] = {
            "Name": name, "Id": nid, "Driver": driver, "EnableIPv6": ipv6, "Options": opts, "IPAM": ipam,
        }  # fmt: skip
        return nid

    def drop_network(self, name):
        for nid in [i for i, n in self.networks.items() if n["Name"] == name]:
            del self.networks[nid]

    # -- views -----------------------------------------------------------
    def builtin(self, fam, name):
        return {"INPUT": self.input, "FORWARD": self.forward}[name][fam]

    def builtin_lines(self, fam, name):
        return [f"-A {name} " + " ".join(_q(t) for t in rule) for rule in self.builtin(fam, name)]

    def input_lines(self, fam):
        return self.builtin_lines(fam, "INPUT")

    def top2(self, fam, name="INPUT"):
        return set(self.builtin(fam, name)[:2])

    def refs(self, fam, name):
        rules = [*self.input[fam], *self.forward[fam]]
        rules += [tuple(shlex.split(r)) for body in self.chains[fam].values() for r in body]
        return sum(1 for r in rules if _target(r) == name)

    def chain_text(self, fam):
        return "\n".join(self.chains[fam].get(FAM[fam].chain, []))

    def state(self):
        return copy.deepcopy((self.input, self.forward, self.chains))

    def restored(self):
        return [fam for fam, _ in self.restores]

    def writes(self):
        """Every state-changing command the script ran (restore attempts included)."""
        return [c for c in self.calls if c[0].endswith("-restore") or (len(c) > 2 and c[2] in ("-I", "-D", "-F", "-X"))]

    # -- run() -----------------------------------------------------------
    def run(self, cmd, *, input_text=None, check=True):
        cmd = list(cmd)
        self.calls.append(tuple(cmd))
        rc, out, err = self._dispatch(cmd[0], tuple(cmd[1:]), input_text)
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def _injected(self, *keys):
        for key in keys:
            if key in self.fail:
                outcome = self.fail[key]
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome
        return None

    def _unexpected(self, prog, args):
        self.unexpected.append((prog, *args))
        return 99, "", "command not modelled by the test fake"

    def _dispatch(self, prog, args, input_text):
        if prog == jdi.DOCKER:
            return self._docker(args)
        if prog == jdi.JOURNALCTL and args[:1] == ("-k",):
            return 0, self.journal, ""
        for fam, f in FAM.items():
            if prog == f.iptables:
                op = args[1] if len(args) > 1 else ""
                return self._injected(fam, f"{fam} {op}") or self._iptables(fam, args)
            if prog == f.restore:
                self.restores.append((fam, args))
                return self._injected(fam, f"{fam} restore") or self._restore(fam, args, input_text)
        return self._unexpected(prog, args)

    def _docker(self, args):
        if args == ("network", "ls", "-q", "--no-trunc", "--filter", "driver=bridge"):
            ids = "".join(f"{nid}\n" for nid, n in self.networks.items() if n["Driver"] == "bridge")
            return self._injected("docker", "docker ls") or (0, ids, "")
        if args == ("network", "ls", "--format", "{{.Driver}} {{.Name}}"):
            lines = "".join(f"{n['Driver']} {n['Name']}\n" for n in self.networks.values())
            return self._injected("docker", "docker ls", "docker drivers") or (0, lines, "")
        if args[:2] == ("network", "inspect") and len(args) > 2:
            hit = self._injected("docker", "docker inspect")
            if hit:
                return hit
            refs = args[2:]
            found = [copy.deepcopy(self.networks[i]) for i in refs if i in self.networks and i not in self.vanish]
            out = json.dumps(found, indent=4) + "\n"
            if len(found) == len(refs):
                return 0, out, ""
            return 1, out, "Error response from daemon: network not found"
        return self._unexpected(jdi.DOCKER, args)

    def _iptables(self, fam, args):
        if args[:1] != ("-w",) or len(args) < 3:
            return self._unexpected(FAM[fam].iptables, args)
        op, rest = args[1], args[2:]
        chains = self.chains[fam]
        if op == "-S" and len(rest) == 1:
            name = rest[0]
            if name in ("INPUT", "FORWARD"):
                policy = "ACCEPT" if name == "INPUT" else "DROP"
                return 0, "\n".join([f"-P {name} {policy}", *self.builtin_lines(fam, name)]) + "\n", ""
            if name in chains:
                return 0, "\n".join([f"-N {name}", *chains[name]]) + "\n", ""
            return 1, "", NO_CHAIN
        if op in ("-C", "-D") and rest[0] in ("INPUT", "FORWARD"):
            rule, rules = tuple(rest[1:]), self.builtin(fam, rest[0])
            if rule not in rules:
                return 1, "", "iptables: Bad rule (does a matching rule exist in that chain?)."
            if op == "-D":
                rules.remove(rule)  # first match, like iptables
            return 0, "", ""
        if op == "-I" and rest[0] in ("INPUT", "FORWARD") and rest[1:2] == ("1",):
            rule = tuple(rest[2:])
            target = _target(rule)
            if target not in chains and target not in BUILTIN_TARGETS:
                return 2, "", f"iptables v1.8.10 (nf_tables): Chain '{target}' does not exist"
            self.builtin(fam, rest[0]).insert(0, rule)
            return 0, "", ""
        if op == "-F" and len(rest) == 1:
            if rest[0] not in chains:
                return 1, "", NO_CHAIN
            chains[rest[0]] = []
            return 0, "", ""
        if op == "-X" and len(rest) == 1:
            name = rest[0]
            if name not in chains:
                return 1, "", NO_CHAIN
            if chains[name]:
                return 1, "", "iptables: Directory not empty."
            if self.refs(fam, name):
                return 1, "", "iptables: Too many links."
            del chains[name]
            return 0, "", ""
        if op == "-L" and rest[1:] in (("-n", "-v", "-x"), ("-n", "-v", "--line-numbers")):
            if rest[0] not in chains:
                return 1, "", NO_CHAIN
            return 0, self._listing(fam, rest[0]), ""
        return self._unexpected(FAM[fam].iptables, args)

    def _listing(self, fam, name):
        refs = self.refs(fam, name)
        lines = [
            f"Chain {name} ({refs} references)",
            "    pkts      bytes target     prot opt in     out     source               destination",
        ]
        for rule in self.chains[fam][name]:
            toks = shlex.split(rule)
            comment = toks[toks.index("--comment") + 1] if "--comment" in toks else ""
            pkts = self.counters[fam].get(comment, 0)
            tail = f"/* {comment} */" if comment else ""
            lines.append(f"{pkts:8d} {pkts * 60:10d} {_target(toks) or '':10} all  --  *      *       {tail}")
        return "\n".join(lines) + "\n"

    def _restore(self, fam, args, text):
        if "--noflush" not in args or "-w" not in args:
            self.unexpected.append((FAM[fam].restore, *args))
            return 2, "", "test fake: a restore without --noflush would replace Docker's rules"
        lines = (text or "").split("\n")
        if len(lines) < 3 or lines[0] != "*filter" or lines[-2:] != ["COMMIT", ""]:
            return 1, "", "iptables-restore: COMMIT expected"
        new: dict[str, list[str]] = {}
        for n, line in enumerate(lines[1:-2], 2):
            m = re.fullmatch(r":(\S+) - \[0:0\]", line)
            if m and m.group(1) not in ("INPUT", "FORWARD", "OUTPUT"):
                new[m.group(1)] = []
                continue
            try:
                toks = shlex.split(line)
            except ValueError:
                return 1, "", f"iptables-restore: line {n} failed"
            if len(toks) < 2 or toks[0] != "-A" or toks[1] not in new:
                return 1, "", f"iptables-restore: line {n} failed"
            new[toks[1]].append(line)
        self.chains[fam].update(new)  # one transaction
        self.counters[fam] = {}
        return 0, "", ""


@pytest.fixture
def host(monkeypatch):
    h = Host()
    h.add_network("bridge", "docker0", subnets=["172.17.0.0/16"])
    h.add_network(HOSTACC, "br-jess-hostacc")
    h.add_network(SCRAPE, "br-jess-scrape")
    h.add_network(GWNET)
    h.add_network(MONNET)
    h.add_network("gateway_nerves_searxng-net")
    h.add_network("host", driver="host")
    h.add_network("none", driver="null")
    monkeypatch.setattr(jdi, "run", h.run)
    monkeypatch.setattr(jdi, "default_gateway", lambda: "10.0.0.1")
    yield h
    assert not h.unexpected, f"script ran commands the fake does not model (or a flushing restore): {h.unexpected}"


def _input_only(text):
    """The repo config with the FORWARD chains off: the INPUT-path tests below predate them."""
    return re.sub(r"(?m)^forward-mode \S+", "forward-mode off", text)


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / "jess-docker-input.conf"
    path.write_text(_input_only(_REPO_CONF.read_text()))
    return path


@pytest.fixture
def fconf(tmp_path):
    """The repo config as shipped (forward-mode observe)."""
    path = tmp_path / "jess-docker-input.conf"
    path.write_text(_REPO_CONF.read_text())
    return path


@pytest.fixture
def mdir(tmp_path):
    path = tmp_path / "textfile"
    path.mkdir()
    return path


def _metrics(mdir):
    out = {}
    for line in (mdir / jdi.METRICS_FILE).read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            out[key] = value
    return out


def _render_live(path):
    """What the watcher renders: the config at `path` against the fake host's networks."""
    nets = jdi.docker_networks()
    return jdi.render(jdi.load_config(path), nets.bridges, nets.subnets)


def _apply_all(cfg=None, bridges=None):
    for rs in jdi.render(cfg or _cfg(), BRIDGES if bridges is None else bridges).rulesets:
        jdi.apply_ruleset(rs)


# --------------------------------------------------------------------------
# 1. parse_config
# --------------------------------------------------------------------------


def test_parse_repo_config():
    cfg = jdi.parse_config(_REPO_CONF.read_text())
    assert cfg.mode == "observe"
    by_net = {a.network: a for a in cfg.allows}
    assert by_net[HOSTACC] == jdi.Allow(HOSTACC, "tcp", (8004, 8053, 8123, 8888))
    assert by_net[SCRAPE] == jdi.Allow(SCRAPE, "tcp", (5050, 8004, 9100, 9400, 19999))
    assert cfg.forward_mode == "observe"
    fwd = set(cfg.forwards)
    # default-bridge DNS (docker run, image builds) is the only router line
    assert jdi.Forward("bridge", "10.0.0.1", "udp", (53,)) in fwd
    assert jdi.Forward("bridge", "10.0.0.1", "tcp", (53,)) in fwd
    assert {f.network for f in fwd if f.dest == "10.0.0.1"} == {"bridge"}
    assert jdi.Forward(HOSTACC, "10.0.0.195", "tcp", (8003, 8004, 8080)) in fwd
    assert jdi.Forward(SCRAPE, "10.0.0.195", "tcp", (8003, 8004, 8080, 9100, 9199, 9400, 19999)) in fwd
    assert not [f for f in fwd if f.dest.startswith("100.")], "nothing may allow the tailnet"
    # the public repo copy carries no satellite addresses (6053 lines are commented out)
    assert not [f for f in fwd if 6053 in f.ports]
    assert jdi.parse_config(_REPO_CONF.read_text(), "10.0.0.1") == cfg  # the router check passes


def test_parse_accepts_and_normalizes():
    cfg = jdi.parse_config(
        "# leading comment\n"
        "  allow\tnet-1.a_B  udp  0053,53,65535,1   # trailing comment -j ACCEPT\n"
        "allow n tcp " + ",".join(str(p) for p in range(1, 16)) + "\n"
        "allow n tcp " + ",".join(["80"] * 20) + "#no space before the comment\n"
        "allow " + "n" * 128 + " tcp 22\n"
        "mode enforce\n"
    )
    assert cfg.mode == "enforce"
    assert cfg.allows == (
        jdi.Allow("net-1.a_B", "udp", (1, 53, 65535)),
        jdi.Allow("n", "tcp", tuple(range(1, 16))),
        jdi.Allow("n", "tcp", (80,)),  # duplicates collapse before the 15-port limit
        jdi.Allow("n" * 128, "tcp", (22,)),
    )


_M = "mode observe\n"
_BAD_CONFIGS = [
    pytest.param("", id="empty"),
    pytest.param("# only a comment\n\n", id="no-mode"),
    pytest.param("allow net tcp 80\n", id="allow-without-mode"),
    pytest.param("mode observe\nmode enforce\n", id="mode-twice"),
    pytest.param("mode observe\nmode observe\n", id="same-mode-twice"),
    pytest.param("mode\n", id="mode-no-value"),
    pytest.param("mode block\n", id="mode-unknown"),
    pytest.param("mode OBSERVE\n", id="mode-uppercase"),
    pytest.param("mode observe enforce\n", id="mode-two-values"),
    pytest.param(_M + "permit net tcp 80\n", id="unknown-keyword"),
    pytest.param(_M + "Allow net tcp 80\n", id="keyword-case"),
    pytest.param(_M + "-A INPUT -j ACCEPT\n", id="raw-rule"),
    pytest.param(_M + 'allow "net" tcp 80\n', id="net-double-quote"),
    pytest.param(_M + "allow 'net' tcp 80\n", id="net-single-quote"),
    pytest.param(_M + 'allow net" tcp 80\n', id="net-inner-quote"),
    pytest.param(_M + "allow my net tcp 80\n", id="net-space"),
    pytest.param(_M + "allow net\ntcp 80\n", id="net-newline"),
    pytest.param(_M + "allow net\x85 tcp 80\n", id="net-nel-linebreak"),
    pytest.param(_M + "allow -net tcp 80\n", id="net-leading-dash"),
    pytest.param(_M + "allow .net tcp 80\n", id="net-leading-dot"),
    pytest.param(_M + "allow net$(id) tcp 80\n", id="net-shell"),
    pytest.param(_M + "allow net;x tcp 80\n", id="net-semicolon"),
    pytest.param(_M + "allow net\x00 tcp 80\n", id="net-nul"),
    pytest.param(_M + "allow net\x1b[0m tcp 80\n", id="net-escape"),
    pytest.param(_M + "allow net\N{LATIN SMALL LETTER O WITH DIAERESIS} tcp 80\n", id="net-non-ascii"),
    pytest.param(_M + "allow " + "n" * 129 + " tcp 80\n", id="net-129-chars"),
    pytest.param(_M + "allow net#x tcp 80\n", id="net-hash-cuts-line"),
    pytest.param(_M + "allow net TCP 80\n", id="proto-uppercase"),
    pytest.param(_M + "allow net icmp 80\n", id="proto-icmp"),
    pytest.param(_M + "allow net all 80\n", id="proto-all"),
    pytest.param(_M + "allow net tcp,udp 80\n", id="proto-list"),
    pytest.param(_M + "allow net sctp 80\n", id="proto-sctp"),
    pytest.param(_M + "allow net tcp 0\n", id="port-0"),
    pytest.param(_M + "allow net tcp 00000\n", id="port-00000"),
    pytest.param(_M + "allow net tcp 65536\n", id="port-65536"),
    pytest.param(_M + "allow net tcp 99999\n", id="port-99999"),
    pytest.param(_M + "allow net tcp 123456\n", id="port-6-digits"),
    pytest.param(_M + "allow net tcp +1\n", id="port-plus"),
    pytest.param(_M + "allow net tcp -1\n", id="port-minus"),
    pytest.param(_M + "allow net tcp 1e3\n", id="port-exponent"),
    pytest.param(_M + "allow net tcp 0x50\n", id="port-hex"),
    pytest.param(_M + "allow net tcp 80-90\n", id="port-range"),
    pytest.param(_M + "allow net tcp 80:90\n", id="port-colon-range"),
    pytest.param(
        _M + "allow net tcp \N{ARABIC-INDIC DIGIT EIGHT}\N{ARABIC-INDIC DIGIT ZERO}\n", id="port-arabic-indic"
    ),
    pytest.param(_M + "allow net tcp \N{FULLWIDTH DIGIT EIGHT}\N{FULLWIDTH DIGIT ZERO}\n", id="port-fullwidth"),
    pytest.param(_M + "allow net tcp 8\N{SUPERSCRIPT TWO}\n", id="port-superscript"),
    pytest.param(_M + "allow net tcp\n", id="ports-missing"),
    pytest.param(_M + "allow net tcp ,\n", id="ports-only-comma"),
    pytest.param(_M + "allow net tcp 80,\n", id="ports-trailing-comma"),
    pytest.param(_M + "allow net tcp ,80\n", id="ports-leading-comma"),
    pytest.param(_M + "allow net tcp 80,,81\n", id="ports-double-comma"),
    pytest.param(_M + "allow net tcp 80, 81\n", id="ports-space"),
    pytest.param(_M + "allow net tcp " + ",".join(str(p) for p in range(1, 17)) + "\n", id="ports-16"),
    pytest.param(_M + "allow net tcp 80 extra\n", id="allow-five-words"),
    pytest.param(_M + "allow net\n", id="allow-two-words"),
]


@pytest.mark.parametrize("text", _BAD_CONFIGS)
def test_parse_rejects(text):
    with pytest.raises(jdi.ConfigError):
        jdi.parse_config(text)


def test_parse_error_names_the_line():
    with pytest.raises(jdi.ConfigError, match=r"^line 3: "):
        jdi.parse_config("mode observe\n\nallow net tcp 70000\n")


def test_network_regex_rejects_trailing_newline():
    # str.split() can't hand the regex a newline today; this guards against a
    # switch to re.match(...$), which would accept "net\n".
    assert jdi._NETWORK_RE.fullmatch("net") is not None
    assert jdi._NETWORK_RE.fullmatch("net\n") is None


_PAYLOADS = ['"', "'", "\\", "$(id)", "`id`", ";", "|", "&", ">", "\r", "\x00", "\x1b", "--", "#x", "\t"]
_PAYLOADS += ["\N{LATIN SMALL LETTER O WITH DIAERESIS}", "\N{ARABIC-INDIC DIGIT ONE}", "\N{NO-BREAK SPACE}"]
_PAYLOADS += [" -j ACCEPT", "\n-A INPUT -j ACCEPT", "\nCOMMIT", "\n*nat", " --dport 22"]
# (network, proto, ports) templates; {p} is replaced by each payload
_POSITIONS = [
    ("net{p}", "tcp", "80"),
    ("{p}net", "tcp", "80"),
    ("net", "tcp{p}", "80"),
    ("net", "udp", "80{p}"),
    ("net", "udp", "{p}80"),
]
_ALLOW_RE = re.compile(
    rf"-A {V4_CHAIN} -i br-0123456789ab -p (tcp|udp) -m multiport "
    r"--dports [1-9][0-9]{0,4}(,[1-9][0-9]{0,4}){0,14} "
    r'-m comment --comment "jess allow [A-Za-z0-9][A-Za-z0-9_.-]{0,127}" -j RETURN'
)


def _shape(text):
    return [re.sub(r'jess-docker-input [0-9a-f]{12}"', 'jess-docker-input D"', line) for line in text.split("\n")]


def test_only_validated_tokens_reach_rendered_text():
    base4, base6 = (set(_shape(rs.text)) for rs in jdi.render(jdi.parse_config("mode enforce\n"), {}).rulesets)
    accepted = 0
    for p in _PAYLOADS:
        for template in _POSITIONS:
            net, proto, ports = (t.replace("{p}", p) for t in template)
            try:
                cfg = jdi.parse_config(f"mode enforce\nallow {net} {proto} {ports}\n")
            except jdi.ConfigError:
                continue
            accepted += 1
            v4, v6 = jdi.render(cfg, {a.network: "br-0123456789ab" for a in cfg.allows}).rulesets
            stray = [line for line in _shape(v4.text) if line not in base4 and not _ALLOW_RE.fullmatch(line)]
            assert not stray, f"payload {p!r} put unvalidated text into the IPv4 rules: {stray!r}"
            assert set(_shape(v6.text)) == base6, f"payload {p!r} changed the IPv6 rules"
    assert accepted >= 3, "the payload list no longer exercises the accepted path (e.g. 'net--', '80#x')"


# --------------------------------------------------------------------------
# 2. render
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", jdi.MODES)
@pytest.mark.parametrize("fmode", jdi.FORWARD_MODES)
def test_render_frame(mode, fmode):
    cfg = _cfg(mode, f"forward-mode {fmode}\nforward {SCRAPE} 10.0.0.195 tcp 8080\n")
    rendered = jdi.render(cfg, BRIDGES, {HOSTACC: "10.9.1.0/24"})
    assert [rs.family for rs in rendered.rulesets] == list(jdi.HOOKS if fmode != "off" else jdi.FAMILIES)
    assert rendered.absent == (() if fmode != "off" else jdi.FORWARD_HOOKS)
    for rs in rendered.rulesets:
        chains = rs.family.chains
        lines = rs.text.split("\n")
        n = len(chains)
        assert lines[: n + 1] == ["*filter", *(f":{c} - [0:0]" for c in chains)]
        assert lines[-2:] == ["COMMIT", ""]
        body = lines[n + 1 : -2]
        owners = [line.split()[1] for line in body]
        assert all(line.startswith("-A ") for line in body) and set(owners) <= set(chains)
        assert owners == sorted(owners, key=chains.index), "each chain's rules are contiguous, main chain first"
        assert rs.counts == tuple((c, owners.count(c)) for c in chains)
        assert rs.rule_count == len(body) == len(_rules(rs))
        assert re.fullmatch(r"[0-9a-f]{12}", rs.digest)
        assert sum(f"jess-docker-input {rs.digest}" in line for line in body) == 1
        first = body[0]
        assert "-m conntrack --ctstate RELATED,ESTABLISHED --ctdir REPLY" in first
        assert f'-m comment --comment "jess-docker-input {rs.digest}"' in first
        assert first.endswith("-j RETURN")


def test_render_observe_blocks_nothing():
    v4, v6 = jdi.render(_cfg("observe"), BRIDGES).rulesets
    for rs in (v4, v6):
        targets = {r.rsplit(" -j ", 1)[1].split()[0] for r in _rules(rs) if " -j " in r}
        assert targets <= {"RETURN", "LOG"}, f"{rs.family.name}: observe mode must not block ({targets})"
        assert f'--log-prefix "{rs.family.log_tag} observe: "' in rs.text
    assert _rule(v4, "jess: invalid").endswith("-j RETURN")
    assert _rule(v4, "jess: not unicast").endswith("-j RETURN")


def test_render_enforce_rejects_the_rest():
    v4, v6 = jdi.render(_cfg("enforce"), BRIDGES).rulesets
    r4 = _main(v4)
    assert r4[-2:] == [f"-A {V4_CHAIN} -p tcp -j REJECT --reject-with tcp-reset", f"-A {V4_CHAIN} -j DROP"]
    assert _rules(v6)[-1] == f"-A {V6_CHAIN} -j DROP"
    assert "REJECT" not in v6.text
    assert _rule(v4, "jess: invalid").endswith("-j DROP")
    assert _rule(v4, "jess: not unicast").endswith("-j DROP")
    for rs in (v4, v6):
        assert f'--log-prefix "{rs.family.log_tag} reject: "' in rs.text

    def pos(needle):
        return next(i for i, r in enumerate(r4) if needle in r)

    # INVALID is dropped before the allow lines; what no line allowed is counted, logged, then refused.
    assert pos("jess: invalid") < pos(f"jess allow {HOSTACC}") < pos(f"jess allow {SCRAPE}")
    assert pos(f"jess allow {SCRAPE}") < pos("jess: unmatched") < pos("jess: logged") < pos("REJECT")


@pytest.mark.parametrize("mode", jdi.MODES)
def test_render_ipv6_has_no_allow_lines(mode):
    v6 = jdi.render(_cfg(mode), BRIDGES).rulesets[1]
    assert "jess allow" not in v6.text and " -i " not in v6.text and "--dports" not in v6.text


def test_render_allow_lines():
    v4 = jdi.render(_cfg(), BRIDGES).rulesets[0]
    assert _rule(v4, f"jess allow {HOSTACC}") == (
        f"-A {V4_CHAIN} -i br-jess-hostacc -p tcp -m multiport --dports 8004,8053,8123,8888 "
        f'-m comment --comment "jess allow {HOSTACC}" -j RETURN'
    )


def test_render_missing_network_gets_no_rule():
    r = jdi.render(_cfg(), {SCRAPE: "br-jess-scrape"})
    assert r.missing == (HOSTACC,)
    v4 = r.rulesets[0].text
    assert f"jess allow {HOSTACC}" not in v4 and "br-jess-hostacc" not in v4
    assert f"jess allow {SCRAPE}" in v4
    assert jdi.render(_cfg(), BRIDGES).missing == ()


def test_render_uses_docker_bridge_names(host):
    r = jdi.render(_cfg(extra=f"allow {GWNET} tcp 8123\n"), jdi.docker_networks().bridges)
    v4 = r.rulesets[0].text
    assert r.missing == ()
    assert "-i br-jess-hostacc -p tcp" in v4  # com.docker.network.bridge.name
    assert f"-i br-{_net_id(GWNET)[:12]} -p tcp" in v4  # else br- + 12 hex of the id


def _digests(cfg, bridges=BRIDGES):
    return tuple(rs.digest for rs in jdi.render(cfg, bridges).rulesets)


def test_digest_is_stable():
    base = _digests(_cfg())
    assert _digests(_cfg()) == base
    same = jdi.parse_config(
        f"# reformatted, same rules\nmode   observe\nallow {HOSTACC} tcp 8888,8123,8053,8004,8004  # dup\n"
        f"\tallow {SCRAPE}  tcp  19999,9400,9100,5050\n"
    )
    assert _digests(same) == base
    assert _digests(_cfg(), {**BRIDGES, "unrelated": "br-0123456789ab"}) == base


def test_digest_changes_with_mode_iface_ports():
    v4, v6 = _digests(_cfg("observe"))
    e4, e6 = _digests(_cfg("enforce"))
    assert e4 != v4 and e6 != v6
    i4, i6 = _digests(_cfg(), {**BRIDGES, HOSTACC: "br-0123456789ab"})
    assert i4 != v4 and i6 == v6  # only the IPv4 chain names interfaces
    fewer = jdi.parse_config(
        f"mode observe\nallow {HOSTACC} tcp 8004,8053,8123\nallow {SCRAPE} tcp 5050,9100,9400,19999\n"
    )
    p4, p6 = _digests(fewer)
    assert p4 != v4 and p6 == v6
    udp = jdi.parse_config(
        f"mode observe\nallow {HOSTACC} udp 8004,8053,8123,8888\nallow {SCRAPE} tcp 5050,9100,9400,19999\n"
    )
    assert _digests(udp)[0] != v4


# --------------------------------------------------------------------------
# 3. docker_networks
# --------------------------------------------------------------------------


def test_docker_networks_names_interfaces(host):
    nets = jdi.docker_networks()
    assert nets.bridges == {
        "bridge": "docker0",
        HOSTACC: "br-jess-hostacc",
        SCRAPE: "br-jess-scrape",
        GWNET: "br-" + _net_id(GWNET)[:12],
        MONNET: "br-" + _net_id(MONNET)[:12],
        "gateway_nerves_searxng-net": "br-" + _net_id("gateway_nerves_searxng-net")[:12],
    }
    assert nets.uncovered == []


def test_docker_networks_empty_or_null_bridge_option(host):
    nid = host.add_network("emptyopt", "")
    host.networks[host.add_network("nullopt")]["Options"] = None
    bridges = jdi.docker_networks().bridges
    assert bridges["emptyopt"] == "br-" + nid[:12]
    assert bridges["nullopt"] == "br-" + _net_id("nullopt")[:12]


def test_docker_networks_uncovered(host):
    host.add_network("virt", "virbr7")
    host.add_network("v6", ipv6=True)
    nets = jdi.docker_networks()
    assert sorted(nets.uncovered) == ["v6", "virt"]
    assert nets.bridges["virt"] == "virbr7"  # still usable in an allow line, just not jumped from INPUT
    assert nets.bridges["v6"] == "br-" + _net_id("v6")[:12]


@pytest.mark.parametrize("bad", ["br-much-too-long-x", 'br-"x', "br-x y", "-evil", "br-x\n"])
def test_docker_networks_skips_bad_interface(host, bad):
    host.add_network("odd", bad)
    nets = jdi.docker_networks()
    assert "odd" not in nets.bridges
    assert "odd" in nets.uncovered
    assert nets.bridges[HOSTACC] == "br-jess-hostacc"


def test_docker0_lookalike_is_uncovered(host):
    host.add_network("lookalike", "docker0x")
    assert "lookalike" in jdi.docker_networks().uncovered


def test_docker_networks_survives_network_removed_mid_inspect(host):
    host.vanish.add(_net_id(GWNET))
    bridges = jdi.docker_networks().bridges
    assert GWNET not in bridges
    assert bridges[HOSTACC] == "br-jess-hostacc" and bridges[SCRAPE] == "br-jess-scrape"


def test_docker_networks_none_listed(host):
    host.networks.clear()
    nets = jdi.docker_networks()
    assert nets.bridges == {} and nets.uncovered == []
    assert not [c for c in host.calls if c[1:3] == ("network", "inspect")]


@pytest.mark.parametrize(
    "key,outcome",
    [
        pytest.param("docker ls", (1, "", "Cannot connect to the Docker daemon"), id="ls-exit-1"),
        pytest.param("docker ls", FileNotFoundError(2, "No such file or directory"), id="ls-no-binary"),
        pytest.param("docker ls", subprocess.TimeoutExpired(["docker"], 60), id="ls-timeout"),
        pytest.param("docker inspect", subprocess.TimeoutExpired(["docker"], 60), id="inspect-timeout"),
        pytest.param("docker inspect", (0, "[{not json", ""), id="inspect-bad-json"),
        pytest.param("docker inspect", (0, '{"Name": "x"}', ""), id="inspect-not-a-list"),
    ],
)
def test_docker_networks_unavailable(host, key, outcome):
    host.fail[key] = outcome
    with pytest.raises(jdi.DockerUnavailable):
        jdi.docker_networks()


# --------------------------------------------------------------------------
# 4. jumps, drift, remove
# --------------------------------------------------------------------------


def test_ensure_jumps_on_top_and_after_tailscaled(host):
    for rs in jdi.render(_cfg(), BRIDGES).rulesets:
        jdi.load_chain(rs)
    for fam, f in zip(FAM, jdi.FAMILIES, strict=True):
        assert jdi.ensure_jumps(f) is True
        assert host.top2(fam) == _ours(fam)
        assert host.input[fam][2:] == [TS_JUMP]
        n = len(host.calls)
        assert jdi.ensure_jumps(f) is False
        assert not [c for c in host.calls[n:] if c[2] in ("-I", "-D")]
        # tailscaled restart: its jump goes back to INPUT position 1
        host.input[fam].remove(TS_JUMP)
        host.input[fam].insert(0, TS_JUMP)
        assert jdi.ensure_jumps(f) is True
        assert host.top2(fam) == _ours(fam)
        assert host.input[fam].count(TS_JUMP) == 1 and len(host.input[fam]) == 3


def test_ensure_jumps_fixes_duplicates(host):
    _apply_all()
    host.input["ipv4"].insert(0, _jump("ipv4", "br-+"))  # an older run left a second copy on top
    assert jdi.ensure_jumps(IPV4) is True
    assert host.top2("ipv4") == _ours("ipv4") and len(host.input["ipv4"]) == 3


def test_chain_in_sync(host):
    v4, v6 = jdi.render(_cfg(), BRIDGES).rulesets
    assert not jdi.chain_in_sync(v4) and not jdi.chain_in_sync(v6)  # chains missing
    jdi.load_chain(v4)
    jdi.load_chain(v6)
    assert jdi.chain_in_sync(v4) and jdi.chain_in_sync(v6)
    e4, e6 = jdi.render(_cfg("enforce"), BRIDGES).rulesets
    assert not jdi.chain_in_sync(e4) and not jdi.chain_in_sync(e6)  # digest changed
    del host.chains["ipv4"][V4_CHAIN][3]
    assert not jdi.chain_in_sync(v4)  # a rule deleted
    host.chains["ipv6"][V6_CHAIN] = []
    assert not jdi.chain_in_sync(v6)  # flushed
    host.chains["ipv6"][V6_CHAIN] = _rules(v6) + [_rules(v6)[0]]
    assert not jdi.chain_in_sync(v6)  # head rule duplicated


def test_remove_only_touches_ours(host):
    _apply_all()
    foreign = [
        ("-i", "br-+", "-j", "ACCEPT"),
        ("-i", "docker0", "-m", "comment", "--comment", "someone-else", "-j", "ts-input"),
    ]
    for fam in FAM:
        host.input[fam] += foreign
    for f in jdi.FAMILIES:
        jdi.remove(f)
    for fam, f in FAM.items():
        assert host.input[fam] == [TS_JUMP, *foreign]
        assert f.chain not in host.chains[fam] and "ts-input" in host.chains[fam]
    before, n = host.state(), len(host.calls)
    for f in jdi.FAMILIES:
        jdi.remove(f)  # idempotent: nothing left to delete, no error
    assert host.state() == before
    assert {c[2] for c in host.calls[n:]} <= {"-C", "-S"}


# --------------------------------------------------------------------------
# Watcher.sync
# --------------------------------------------------------------------------


def test_sync_applies_then_leaves_healthy_chains(host, conf, mdir):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    assert host.restored() == ["ipv4", "ipv6"]
    for fam, rs in zip(FAM, _render_live(conf).rulesets, strict=True):
        assert host.top2(fam) == _ours(fam)
        assert host.chain_text(fam) == "\n".join(_main(rs))
    m = _metrics(mdir)
    assert m["jess_docker_input_check_ok"] == "1"
    assert m["jess_docker_input_enforcing"] == "0"
    assert m["jess_docker_input_networks_missing"] == "0"
    assert m["jess_docker_input_networks_uncovered"] == "0"
    n = len(host.writes())
    w.sync()
    assert len(host.writes()) == n, "a healthy chain must not be reloaded (it would reset its counters)"


def test_sync_repairs_displaced_jump_without_reload(host, conf, mdir, capsys):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    host.counters["ipv4"]["jess: unmatched"] = 42
    host.input["ipv4"].insert(0, TS_JUMP)  # tailscaled restarted
    host.input["ipv6"].remove(_jump("ipv6", "docker0"))  # a jump deleted by hand
    capsys.readouterr()
    w.sync()
    assert host.restored() == ["ipv4", "ipv6"], "a jump repair must not reload the chain"
    assert host.top2("ipv4") == _ours("ipv4") and host.top2("ipv6") == _ours("ipv6")
    assert host.counters["ipv4"]["jess: unmatched"] == 42
    err = capsys.readouterr().err
    assert "ipv4: jumps placed at the top of INPUT" in err and "ipv6: jumps placed" in err
    assert "applied" not in err


def test_sync_detects_rule_replaced_in_place(host, conf, mdir):
    # `iptables -R` keeps the rule count and the head digest; only the read-back
    # copy of the chain from the last load can tell.
    w = jdi.Watcher(conf, mdir)
    w.sync()
    rules = host.chains["ipv4"][V4_CHAIN]
    allow = next(i for i, r in enumerate(rules) if "jess allow" in r)
    rules[allow] = rules[allow].replace("--dports 8004,8053,8123,8888", "--dports 1-65535")
    assert jdi.chain_in_sync(_render_live(conf).rulesets[0])
    w.sync()
    assert host.restored() == ["ipv4", "ipv6", "ipv4"]
    assert "--dports 1-65535" not in host.chain_text("ipv4")


def test_sync_reloads_only_drifted_family(host, conf, mdir):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    del host.chains["ipv6"][V6_CHAIN][-1]
    w.sync()
    assert host.restored() == ["ipv4", "ipv6", "ipv6"]
    host.chains["ipv4"][V4_CHAIN] = []  # iptables -F JESS-DOCKER-IN
    w.sync()
    assert host.restored()[3:] == ["ipv4"]
    # a recreated network gets a new id, so a new br-<id> bridge: IPv4 reloads, IPv6 doesn't
    new_id = _net_id("recreated")
    host.drop_network(GWNET)
    host.add_network(GWNET, net_id=new_id)
    w.sync()
    assert host.restored()[4:] == ["ipv4"]
    assert f"-i br-{new_id[:12]} " in host.chain_text("ipv4")
    assert f"-i br-{_net_id(GWNET)[:12]} " not in host.chain_text("ipv4")


def test_sync_force_and_mode_change(host, conf, mdir):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    w.sync(force=True)
    assert host.restored() == ["ipv4", "ipv6"] * 2
    conf.write_text(conf.read_text().replace("mode observe", "mode enforce"))
    w.sync()
    assert host.restored() == ["ipv4", "ipv6"] * 3
    assert _metrics(mdir)["jess_docker_input_enforcing"] == "1"
    assert host.chains["ipv4"][V4_CHAIN][-1] == f"-A {V4_CHAIN} -j DROP"


def test_sync_missing_then_created_network(host, conf, mdir, capsys):
    host.drop_network(HOSTACC)
    w = jdi.Watcher(conf, mdir)
    w.sync()
    assert "br-jess-hostacc" not in host.chain_text("ipv4")
    assert _metrics(mdir)["jess_docker_input_networks_missing"] == "1"
    assert f"missing_networks={HOSTACC}" in capsys.readouterr().err
    host.add_network(HOSTACC, "br-jess-hostacc")
    w.sync()
    assert "-i br-jess-hostacc " in host.chain_text("ipv4")
    assert _metrics(mdir)["jess_docker_input_networks_missing"] == "0"
    assert host.restored() == ["ipv4", "ipv6", "ipv4"]


def test_sync_reports_uncovered_once(host, conf, mdir, capsys):
    host.add_network("v6net", ipv6=True)
    w = jdi.Watcher(conf, mdir)
    w.sync()
    w.sync()
    assert _metrics(mdir)["jess_docker_input_networks_uncovered"] == "1"
    assert capsys.readouterr().err.count("networks the chains do not cover: v6net") == 1


def _break_config(host, conf):
    conf.write_text("mode observe\nallow bad'name tcp 22\n")


def _remove_config(host, conf):
    conf.unlink()


def _break_docker(host, conf):
    host.fail["docker"] = (1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")


def _break_iptables(host, conf):
    host.fail.update(ipv4=(4, "", LOCKED), ipv6=(4, "", LOCKED))


def _drift_and_break_restore(host, conf):
    del host.chains["ipv4"][V4_CHAIN][2]
    host.fail["ipv4 restore"] = (1, "", "iptables-restore: line 4 failed")


@pytest.mark.parametrize(
    "breakage,logged",
    [
        pytest.param(_break_config, "config error, chains left as they are: line 2: bad network name", id="config"),
        pytest.param(_remove_config, "config error, chains left as they are: cannot read", id="config-gone"),
        pytest.param(_break_docker, "docker unavailable (CalledProcessError), chains left as they are", id="docker"),
        pytest.param(_break_iptables, "iptables failed (CalledProcessError), chains left as they are", id="iptables"),
        pytest.param(_drift_and_break_restore, "iptables failed (CalledProcessError)", id="restore"),
    ],
)
def test_sync_failure_keeps_chains(host, conf, mdir, capsys, breakage, logged):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    capsys.readouterr()
    breakage(host, conf)
    before = host.state()
    for _ in range(3):
        w.sync()
    assert host.state() == before, "a failed sync must leave the chains and jumps as they are"
    assert host.chains["ipv4"][V4_CHAIN] and host.chains["ipv6"][V6_CHAIN]
    assert _metrics(mdir)["jess_docker_input_check_ok"] == "0"
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and logged in err[0], f"expected one log line containing {logged!r}, got {err!r}"


def test_sync_logs_each_problem_once(host, conf, mdir, capsys):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    capsys.readouterr()
    _break_docker(host, conf)
    w.sync()
    w.sync()
    conf.write_text("mode maybe\n")  # config errors are found before docker is asked
    w.sync()
    w.sync()
    del host.fail["docker"]
    conf.write_text(_input_only(_REPO_CONF.read_text()))
    w.sync()
    w.sync()
    assert _metrics(mdir)["jess_docker_input_check_ok"] == "1"
    _break_docker(host, conf)
    w.sync()
    kinds = [line.split(": ", 1)[1].split(" (")[0].split(",")[0] for line in capsys.readouterr().err.splitlines()]
    assert kinds == ["docker unavailable", "config error", "recovered", "docker unavailable"]
    assert host.restored() == ["ipv4", "ipv6"], "recovering with an unchanged ruleset must not reload"


def test_sync_dockerd_lost_mid_listing_keeps_chains(host, conf, mdir):
    conf.write_text(conf.read_text().replace("mode observe", "mode enforce"))
    w = jdi.Watcher(conf, mdir)
    w.sync()
    before = host.state()
    host.fail["docker inspect"] = (1, "[]\n", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock.")
    w.sync()
    assert host.state() == before
    assert _metrics(mdir)["jess_docker_input_check_ok"] == "0"


# --------------------------------------------------------------------------
# 6. metrics
# --------------------------------------------------------------------------


def test_format_metrics():
    text = jdi.format_metrics(
        [
            ("jess_docker_input_unmatched_packets_total", {"family": "ipv4"}, 1234567),
            ("jess_docker_input_unmatched_packets_total", {"family": "ipv6"}, 0),
            ("jess_docker_input_last_check_timestamp_seconds", {}, 1_800_000_000.25),
            ("jess_docker_input_info", {"sha256": "0123456789ab"}, 1),
        ]
    )
    lines = text.splitlines()
    assert text.endswith("\n") and "e+" not in text
    assert 'jess_docker_input_unmatched_packets_total{family="ipv4"} 1234567' in lines
    assert 'jess_docker_input_unmatched_packets_total{family="ipv6"} 0' in lines
    assert "jess_docker_input_last_check_timestamp_seconds 1800000000.250" in lines
    assert 'jess_docker_input_info{sha256="0123456789ab"} 1' in lines
    for name, mtype in [
        ("jess_docker_input_unmatched_packets_total", "counter"),
        ("jess_docker_input_last_check_timestamp_seconds", "gauge"),
        ("jess_docker_input_info", "gauge"),
    ]:
        help_i = lines.index(f"# HELP {name} {jdi.METRICS[name][1]}")
        assert lines[help_i + 1] == f"# TYPE {name} {mtype}"
        assert text.count(f"# TYPE {name} ") == 1
        first_sample = next(
            i for i, line in enumerate(lines) if line.startswith(name + " ") or line.startswith(name + "{")
        )
        assert first_sample == help_i + 2
    sample_re = re.compile(r'[a-z_]+(\{[a-z0-9_]+="[^"]*"(,[a-z0-9_]+="[^"]*")*\})? -?[0-9]+(\.[0-9]+)?')
    assert all(sample_re.fullmatch(line) for line in lines if not line.startswith("#"))


def test_watcher_metrics_counters(host, conf, mdir):
    w = jdi.Watcher(conf, mdir)
    w.sync()
    host.counters["ipv4"].update({"jess: unmatched": 1234567, "jess: logged": 89})
    host.counters["ipv6"].update({"jess: unmatched": 3})
    w.metrics()
    m = _metrics(mdir)
    assert m['jess_docker_input_unmatched_packets_total{family="ipv4"}'] == "1234567"
    assert m['jess_docker_input_logged_packets_total{family="ipv4"}'] == "89"
    assert m['jess_docker_input_unmatched_packets_total{family="ipv6"}'] == "3"
    assert m['jess_docker_input_logged_packets_total{family="ipv6"}'] == "0"
    assert m[f'jess_docker_input_info{{sha256="{hashlib.sha256(_SCRIPT.read_bytes()).hexdigest()[:12]}"}}'] == "1"
    forward_only = {
        "jess_docker_input_forward_unmatched_packets_total",
        "jess_docker_input_forward_logged_packets_total",
    }
    assert set(jdi.METRICS) - forward_only == {k.split("{")[0] for k in m}  # forward-mode off
    assert m['jess_docker_input_spoofed_packets_total{path="input"}'] == "0"
    assert m["jess_docker_input_forward_active"] == "0"


class _OsSpy:
    """Stands in for the module's `os`; records every os.replace."""

    def __init__(self, fail=False):
        self.replaced = []
        self.fail = fail

    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, src, dst):
        src, dst = Path(src), Path(dst)
        self.replaced.append((src, dst, stat.S_IMODE(src.stat().st_mode), src.read_text()))
        if self.fail:
            raise PermissionError(13, "Permission denied")
        os.replace(src, dst)


def test_write_metrics_atomic(tmp_path, monkeypatch):
    spy = _OsSpy()
    monkeypatch.setattr(jdi, "os", spy)
    final = tmp_path / jdi.METRICS_FILE
    final.write_text("old\n")
    jdi.write_metrics(tmp_path, "jess_docker_input_check_ok 1\n")
    ((src, dst, mode, content),) = spy.replaced
    assert src.parent == tmp_path and src.name.startswith(f".{jdi.METRICS_FILE}.") and src.name.endswith(".tmp")
    assert dst == final
    assert mode == 0o644 and content == "jess_docker_input_check_ok 1\n"
    assert final.read_text() == "jess_docker_input_check_ok 1\n"
    assert stat.S_IMODE(final.stat().st_mode) == 0o644
    assert sorted(p.name for p in tmp_path.iterdir()) == [jdi.METRICS_FILE]


def test_write_metrics_failures_do_not_raise(tmp_path, monkeypatch, capsys):
    jdi.write_metrics(None, "x 1\n")  # no --metrics-dir: no-op
    jdi.write_metrics(tmp_path / "missing", "x 1\n")
    assert not (tmp_path / "missing").exists()
    assert "cannot write metrics" in capsys.readouterr().err
    monkeypatch.setattr(jdi, "os", _OsSpy(fail=True))
    jdi.write_metrics(tmp_path, "x 1\n")
    assert list(tmp_path.iterdir()) == [], "a failed replace must not leave its temp file behind"
    assert "cannot write metrics: Permission denied" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 7. main()
# --------------------------------------------------------------------------


@pytest.mark.parametrize("result", ["signal", "exit-code", "core-dump", "watchdog", "timeout"])
def test_main_remove_after_crash_keeps_chains(host, conf, monkeypatch, capsys, result):
    assert jdi.main(["apply", "--config", str(conf)]) == 0
    before, n = host.state(), len(host.calls)
    monkeypatch.setenv("SERVICE_RESULT", result)
    assert jdi.main(["remove", "--only-if-clean-stop"]) == 0
    assert host.calls[n:] == [] and host.state() == before
    assert "keeping the chains for the restart" in capsys.readouterr().err


@pytest.mark.parametrize(
    "result,argv",
    [
        pytest.param("success", ["remove", "--only-if-clean-stop"], id="clean-stop"),
        pytest.param(None, ["remove", "--only-if-clean-stop"], id="unset"),
        pytest.param("signal", ["remove"], id="manual-remove"),
    ],
)
def test_main_remove(host, conf, monkeypatch, result, argv):
    assert jdi.main(["apply", "--config", str(conf)]) == 0
    if result is None:
        monkeypatch.delenv("SERVICE_RESULT", raising=False)
    else:
        monkeypatch.setenv("SERVICE_RESULT", result)
    assert jdi.main(argv) == 0
    for fam, f in FAM.items():
        assert host.input[fam] == [TS_JUMP]
        assert f.chain not in host.chains[fam]


def test_main_check_prints_one_line(host, conf, capsys):
    assert jdi.main(["check", "--config", str(conf)]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    (line,) = err.splitlines()
    assert re.fullmatch(
        r"jess-docker-input: config ok: mode=observe allow_rules=4 ipv4=[0-9a-f]{12} ipv6=[0-9a-f]{12}", line
    )
    assert host.writes() == []


def test_main_render_and_apply(host, conf, capsys):
    assert jdi.main(["render", "--config", str(conf)]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"# {IPV4.restore} --noflush\n*filter\n:{V4_CHAIN} - [0:0]\n")
    assert f"COMMIT\n# {IPV6.restore} --noflush\n*filter\n:{V6_CHAIN} - [0:0]\n" in out
    assert out.endswith("COMMIT\n")
    assert host.writes() == []
    assert jdi.main(["apply", "--config", str(conf)]) == 0
    assert host.restored() == ["ipv4", "ipv6"]
    assert all(args == ("-w", "--noflush") for _, args in host.restores)
    for fam in FAM:
        assert host.top2(fam) == _ours(fam)
        assert host.chain_text(fam) in out  # what render printed is what apply loaded


@pytest.mark.parametrize("cmd", ["check", "render", "apply"])
def test_main_config_error_exits_2(host, tmp_path, capsys, cmd):
    bad = tmp_path / "bad.conf"
    bad.write_text("mode observe\nallow net tcp 70000\n")
    assert jdi.main([cmd, "--config", str(bad)]) == 2
    assert jdi.main([cmd, "--config", str(tmp_path / "absent.conf")]) == 2
    assert host.writes() == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("cmd", ["check", "render", "apply"])
def test_main_docker_unavailable_exits_3(host, conf, capsys, cmd):
    host.fail["docker"] = (1, "", "Cannot connect to the Docker daemon")
    assert jdi.main([cmd, "--config", str(conf)]) == 3
    assert host.writes() == []
    assert "docker unavailable" in capsys.readouterr().err


def test_main_apply_iptables_failure_exits_1(host, conf, capsys):
    host.fail["ipv4 restore"] = (1, "", "iptables-restore: line 5 failed")
    assert jdi.main(["apply", "--config", str(conf)]) == 1
    assert f"{IPV4.restore} failed (exit 1): iptables-restore: line 5 failed" in capsys.readouterr().err
    assert V4_CHAIN not in host.chains["ipv4"]


def test_main_status(host, conf, capsys):
    assert jdi.main(["status"]) == 1
    out = capsys.readouterr().out
    assert f"script sha256: {hashlib.sha256(_SCRIPT.read_bytes()).hexdigest()}" in out
    assert f"chain {V4_CHAIN} not present" in out and f"chain {V6_CHAIN} not present" in out
    assert jdi.main(["apply", "--config", str(conf)]) == 0
    capsys.readouterr()
    assert jdi.main(["status"]) == 0
    assert f"Chain {V4_CHAIN} (2 references)" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 8. summary
# --------------------------------------------------------------------------

_KLOG_LINES = [
    pytest.param(
        "jess-docker-in observe: IN=br-1f56b9570309 OUT= MAC=aa:bb SRC=172.25.0.4 DST=172.25.0.1 LEN=60 TOS=0x00 "
        "PREC=0x00 TTL=64 ID=1 DF PROTO=TCP SPT=40000 DPT=22 WINDOW=64240 RES=0x00 SYN URGP=0",
        ("jess-docker-in", "observe", "br-1f56b9570309", "172.25.0.1", "TCP", "22"),
        id="v4-tcp",
    ),
    pytest.param(
        "jess-docker-in reject: IN=br-jess-hostacc OUT= MAC=aa:bb SRC=172.30.0.2 DST=172.30.0.1 LEN=84 TOS=0x00 "
        "PREC=0x00 TTL=64 ID=7 DF PROTO=ICMP TYPE=8 CODE=0 ID=3 SEQ=1",
        ("jess-docker-in", "reject", "br-jess-hostacc", "172.30.0.1", "ICMP", None),
        id="v4-icmp",
    ),
    pytest.param(
        "jess-docker-in observe: IN=docker0 OUT= MAC=aa:bb SRC=172.17.0.2 DST=172.17.0.1 LEN=56 TOS=0x00 PREC=0x00 "
        "TTL=64 ID=9 DF PROTO=UDP SPT=51000 DPT=53 LEN=36",
        ("jess-docker-in", "observe", "docker0", "172.17.0.1", "UDP", "53"),
        id="v4-udp",
    ),
    pytest.param(
        "jess-docker-in6 observe: IN=br-1f56b9570309 OUT= MAC=aa:bb SRC=fe80:0000:0000:0000:0042:acff:fe19:0004 "
        "DST=fe80:0000:0000:0000:0042:acff:fe19:0001 LEN=80 TC=0 HOPLIMIT=64 FLOWLBL=0 PROTO=TCP SPT=40000 DPT=445 "
        "WINDOW=64800 RES=0x00 SYN URGP=0",
        ("jess-docker-in6", "observe", "br-1f56b9570309", "fe80:0000:0000:0000:0042:acff:fe19:0001", "TCP", "445"),
        id="v6-tcp",
    ),
]


@pytest.mark.parametrize("line,groups", _KLOG_LINES)
def test_klog_regex(line, groups):
    m = jdi._KLOG_RE.search(line)
    assert m is not None and m.groups() == groups


def test_klog_regex_ignores_other_lines():
    assert jdi._KLOG_RE.search("UFW BLOCK IN=eth0 OUT= SRC=10.0.0.9 DST=10.0.0.1 PROTO=TCP DPT=22") is None
    assert jdi._KLOG_RE.search("jess-docker-in started") is None


def test_summary_groups_by_network(host):
    tcp, icmp, udp, v6 = (p.values[0] for p in _KLOG_LINES)
    hostacc_tcp = tcp.replace("IN=br-1f56b9570309", "IN=br-jess-hostacc")
    host.journal = "\n".join([tcp, tcp, hostacc_tcp, icmp, udp, v6, "unrelated kernel line"]) + "\n"
    out = jdi.summary(6).splitlines()
    assert out[0].split() == ["count", "path", "kind", "from", "network", "to", "proto", "port"]
    rows = [row.split() for row in out[1:]]
    assert rows[0] == ["2", "in", "observe", "br-1f56b9570309", "172.25.0.1", "TCP", "22"]  # most common first
    assert ["1", "in", "observe", HOSTACC, "172.25.0.1", "TCP", "22"] in rows  # bridge mapped back to its network
    assert ["1", "in", "reject", HOSTACC, "172.30.0.1", "ICMP", "-"] in rows
    assert ["1", "in", "observe", "bridge", "172.17.0.1", "UDP", "53"] in rows
    assert ["1", "in6", "observe", "br-1f56b9570309", "fe80:0000:0000:0000:0042:acff:fe19:0001", "TCP", "445"] in rows
    assert len(rows) == 5
    (journal_call,) = [c for c in host.calls if c[0] == jdi.JOURNALCTL]
    assert "--since=-6h" in journal_call and "-k" in journal_call


def test_summary_without_docker_or_lines(host):
    host.fail["docker"] = (1, "", "down")
    host.journal = _KLOG_LINES[1].values[0] + "\n"
    assert "br-jess-hostacc" in jdi.summary(24)  # raw interface when docker can't map it
    host.journal = ""
    assert jdi.summary(24) == "no jess-docker log lines in the last 24 h"


# --------------------------------------------------------------------------
# 9. FORWARD chains (forward-mode, forward lines) and the source check
#    (docs/internal/CONTAINER_LAN_REACHABILITY.md)
# --------------------------------------------------------------------------

_FWD_M = "mode observe\nforward-mode observe\n"


def _fcfg(fmode="observe", mode="observe", extra=""):
    return jdi.parse_config(
        f"mode {mode}\nforward-mode {fmode}\n"
        f"forward {HOSTACC} 10.0.0.195 tcp 8003,8080\n"
        f"forward {SCRAPE} 10.0.0.248 tcp 8777\n"
        f"forward bridge 10.0.0.1 udp 53\n{extra}",
        "10.0.0.1",
    )


FBRIDGES = {**BRIDGES, "bridge": "docker0"}
FSUBNETS = {HOSTACC: "10.9.1.0/24", SCRAPE: "10.9.2.0/24", "bridge": "172.17.0.0/16"}


def _fwd(rendered):
    v4, v6 = rendered.rulesets[2:]
    assert (v4.family, v6.family) == (FWD4, FWD6)
    return v4, v6


def test_parse_forward_lines():
    cfg = _fcfg(extra="forward n 10.0.1.0/24 tcp 6053\nforward n 100.100.1.0/24 udp 0053,53\n")
    assert cfg.forward_mode == "observe"
    assert cfg.forwards[0] == jdi.Forward(HOSTACC, "10.0.0.195", "tcp", (8003, 8080))  # /32 written as an address
    assert cfg.forwards[-2] == jdi.Forward("n", "10.0.1.0/24", "tcp", (6053,))
    assert cfg.forwards[-1] == jdi.Forward("n", "100.100.1.0/24", "udp", (53,))
    assert jdi.parse_config("mode observe\n").forward_mode == "off"  # a config without the line keeps working
    assert jdi.parse_config("mode observe\nforward n 10.0.0.195/32 tcp 1\n").forwards[0].dest == "10.0.0.195"


_BAD_FORWARD = [
    pytest.param(_FWD_M + "forward-mode on\n", id="fmode-unknown"),
    pytest.param(_FWD_M + "forward-mode off\n", id="fmode-twice"),
    pytest.param("mode observe\nforward-mode\n", id="fmode-no-value"),
    pytest.param(_FWD_M + "forward n 10.0.0.5 tcp\n", id="four-words"),
    pytest.param(_FWD_M + "forward n 10.0.0.5 tcp 80 x\n", id="six-words"),
    pytest.param(_FWD_M + "forward n;x 10.0.0.5 tcp 80\n", id="bad-network"),
    pytest.param(_FWD_M + "forward n 10.0.0.5 icmp 80\n", id="bad-proto"),
    pytest.param(_FWD_M + "forward n 10.0.0.5 tcp 0\n", id="port-0"),
    pytest.param(_FWD_M + "forward n 10.0.0.5 tcp 80,\n", id="port-trailing-comma"),
    pytest.param(_FWD_M + "forward n 10.0.0.0/23 tcp 80\n", id="prefix-too-short"),
    pytest.param(_FWD_M + "forward n 0.0.0.0/0 tcp 80\n", id="everything"),
    pytest.param(_FWD_M + "forward n 10.0.0.5/24 tcp 80\n", id="host-bits"),
    pytest.param(_FWD_M + "forward n 10.0.0.256 tcp 80\n", id="octet-256"),
    pytest.param(_FWD_M + "forward n 010.0.0.5 tcp 80\n", id="leading-zero"),
    pytest.param(_FWD_M + "forward n 10.0.0 tcp 80\n", id="three-octets"),
    pytest.param(_FWD_M + "forward n 10.0.0.5/33 tcp 80\n", id="prefix-33"),
    pytest.param(_FWD_M + "forward n fd00::1 tcp 80\n", id="ipv6"),
    pytest.param(_FWD_M + "forward n example.com tcp 80\n", id="hostname"),
    pytest.param(_FWD_M + "forward n 1.1.1.1 tcp 443\n", id="public-address"),
    pytest.param(_FWD_M + "forward n 10.0.0.1 tcp 80\n", id="router-admin"),
    pytest.param(_FWD_M + "forward n 10.0.0.1 udp 53,5351\n", id="router-natpmp"),
    pytest.param(_FWD_M + "forward n 10.0.0.0/24 udp 5351\n", id="router-via-subnet"),
]


@pytest.mark.parametrize("text", _BAD_FORWARD)
def test_parse_forward_rejects(text):
    with pytest.raises(jdi.ConfigError, match=r"^line \d+: "):
        jdi.parse_config(text, "10.0.0.1")


def test_parse_router_check_needs_the_gateway():
    text = "mode observe\nforward n 10.0.0.1 tcp 80\n"
    with pytest.raises(jdi.ConfigError, match="router"):
        jdi.parse_config(text, "10.0.0.1")
    assert jdi.parse_config(text, None).forwards  # gateway unknown: no router check
    assert jdi.parse_config("mode observe\nforward n 10.0.0.0/24 tcp 53\n", "10.0.0.1").forwards  # DNS is fine


def test_load_config_reads_the_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(jdi, "default_gateway", lambda: "10.0.0.1")
    path = tmp_path / "c.conf"
    path.write_text("mode observe\nforward n 10.0.0.1 tcp 443\n")
    with pytest.raises(jdi.ConfigError, match="router"):
        jdi.load_config(path)


def test_default_gateway(tmp_path, monkeypatch):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "br-x\t00001AAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "enp7s0\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )
    monkeypatch.setattr(jdi, "ROUTE_FILE", route)
    assert jdi.default_gateway() == "10.0.0.1"
    route.write_text("Iface\tDestination\tGateway\nbr-x\t00001AAC\t00000000\n")
    assert jdi.default_gateway() is None
    monkeypatch.setattr(jdi, "ROUTE_FILE", tmp_path / "absent")
    assert jdi.default_gateway() is None


_FWD_ALLOW_RE = re.compile(
    rf"-A {jdi.FWD_LAN} -i br-0123456789ab -p (tcp|udp) -m conntrack "
    r"--ctorigdst (10|172|192|100|169)(\.[0-9]{1,3}){3}(/(2[4-9]|3[0-2]))? --ctorigdstport [1-9][0-9]{0,4} "
    r'-m comment --comment "jess forward [A-Za-z0-9][A-Za-z0-9_.-]{0,127}" -j RETURN'
)
_FWD_POSITIONS = [
    ("net{p}", "10.0.0.5", "tcp", "80"),
    ("net", "10.0.0.5{p}", "tcp", "80"),
    ("net", "{p}10.0.0.5", "tcp", "80"),
    ("net", "10.0.0.0/24{p}", "udp", "80"),
    ("net", "10.0.0.5", "tcp{p}", "80"),
    ("net", "10.0.0.5", "udp", "80{p}"),
]


def test_only_validated_tokens_reach_forward_rules():
    def shape(rs):
        return {
            re.sub(r'jess-docker-input [0-9a-f]{12}"', 'jess-docker-input D"', line) for line in rs.text.split("\n")
        }

    base4, base6 = (shape(rs) for rs in _fwd(jdi.render(jdi.parse_config("mode enforce\nforward-mode enforce\n"), {})))
    accepted = 0
    for p in _PAYLOADS:
        for template in _FWD_POSITIONS:
            net, dest, proto, ports = (t.replace("{p}", p) for t in template)
            try:
                cfg = jdi.parse_config(f"mode enforce\nforward-mode enforce\nforward {net} {dest} {proto} {ports}\n")
            except jdi.ConfigError:
                continue
            accepted += 1
            v4, v6 = _fwd(jdi.render(cfg, {f.network: "br-0123456789ab" for f in cfg.forwards}))
            stray = [line for line in shape(v4) - base4 if not _FWD_ALLOW_RE.fullmatch(line)]
            assert not stray, f"payload {p!r} put unvalidated text into the FORWARD rules: {stray!r}"
            assert shape(v6) == base6
    assert accepted >= 3


@pytest.mark.parametrize("fmode", ["observe", "enforce"])
def test_render_forward_order(fmode):
    v4, v6 = _fwd(jdi.render(_fcfg(fmode), FBRIDGES, FSUBNETS))
    main = _main(v4)
    assert "--ctdir REPLY" in main[0]  # answers to LAN clients of published ports, first
    src = [i for i, r in enumerate(main) if "jess: source" in r]
    dnat = next(i for i, r in enumerate(main) if "--ctstate DNAT" in r)
    bridge_return = main.index(f"-A {FWD4.chain} -o br-+ -j RETURN")
    private = [i for i, r in enumerate(main) if " -d " in r]
    assert src and max(src) < dnat < bridge_return < min(private), "source check, then published ports, then LAN"
    assert main[dnat].endswith(f"-j {jdi.FWD_LAN}")
    assert [r.split(" -d ")[1].split()[0] for r in (main[i] for i in private)] == [str(n) for n in jdi.PRIVATE_V4]
    assert all(r.endswith(f"-j {jdi.FWD_LAN}") for r in (main[i] for i in private))
    assert main[-1] == f"-A {FWD4.chain} -d 192.0.0.0/24 -j {jdi.FWD_LAN}"  # nothing after: the internet passes
    assert f"-A {FWD4.chain} -i docker0 ! -s 172.17.0.0/16 " in main[src[0]]  # sorted by network name: bridge first


def test_render_forward_allow_lines_one_per_port():
    v4, _ = _fwd(jdi.render(_fcfg(), FBRIDGES, FSUBNETS))
    allows = [r for r in _rules(v4) if "jess forward" in r]
    assert allows == [
        f"-A {jdi.FWD_LAN} -i br-jess-hostacc -p tcp -m conntrack --ctorigdst 10.0.0.195 --ctorigdstport 8003 "
        f'-m comment --comment "jess forward {HOSTACC}" -j RETURN',
        f"-A {jdi.FWD_LAN} -i br-jess-hostacc -p tcp -m conntrack --ctorigdst 10.0.0.195 --ctorigdstport 8080 "
        f'-m comment --comment "jess forward {HOSTACC}" -j RETURN',
        f"-A {jdi.FWD_LAN} -i br-jess-scrape -p tcp -m conntrack --ctorigdst 10.0.0.248 --ctorigdstport 8777 "
        f'-m comment --comment "jess forward {SCRAPE}" -j RETURN',
        f"-A {jdi.FWD_LAN} -i docker0 -p udp -m conntrack --ctorigdst 10.0.0.1 --ctorigdstport 53 "
        f'-m comment --comment "jess forward bridge" -j RETURN',
    ]


def test_render_forward_observe_blocks_nothing():
    for rs in _fwd(jdi.render(_fcfg("observe", "enforce"), FBRIDGES, FSUBNETS)):
        targets = {r.rsplit(" -j ", 1)[1].split()[0] for r in _rules(rs) if " -j " in r}
        assert targets <= {"RETURN", "LOG", jdi.FWD_LAN, jdi.FWD_SPOOF}, targets
        assert "observe: " in rs.text and "reject: " not in rs.text  # forward-mode, not mode, decides


def test_render_forward_enforce():
    v4, v6 = _fwd(jdi.render(_fcfg("enforce", "observe"), FBRIDGES, FSUBNETS))
    lan = [r for r in _rules(v4) if r.startswith(f"-A {jdi.FWD_LAN} ")]
    spoof = [r for r in _rules(v4) if r.startswith(f"-A {jdi.FWD_SPOOF} ")]
    assert lan[0].endswith("-j DROP") and "--ctstate INVALID" in lan[0]
    assert lan[-1] == f"-A {jdi.FWD_LAN} -j REJECT --reject-with icmp-admin-prohibited"
    assert lan[-3].endswith('--comment "jess: fwd unmatched"') and '"jess: fwd logged"' in lan[-2]
    assert spoof[-1] == f"-A {jdi.FWD_SPOOF} -j DROP"
    assert _main(v6)[-1] == f"-A {FWD6.chain} -j DROP" and "REJECT" not in v6.text
    assert '--log-prefix "jess-docker-fwd reject: "' in v4.text
    assert '--log-prefix "jess-docker-spoof reject: "' in v4.text
    assert '--log-prefix "jess-docker-fwd6 reject: "' in v6.text
    in4 = jdi.render(_fcfg("enforce", "observe"), FBRIDGES, FSUBNETS).rulesets[0]
    assert "-j DROP" not in in4.text and "reject: " not in in4.text  # INPUT still observes


def test_render_forward_hashlimit_names_fit():
    rendered = jdi.render(_fcfg(), FBRIDGES, FSUBNETS)
    names = set(re.findall(r"--hashlimit-name (\S+)", "".join(rs.text for rs in rendered.rulesets)))
    assert len(names) == 6 and all(len(n) <= 15 for n in names), names  # xt_hashlimit: 15 chars max
    prefixes = re.findall(r'--log-prefix "([^"]+)"', "".join(rs.text for rs in rendered.rulesets))
    assert prefixes and all(len(p) <= 29 for p in prefixes), prefixes  # LOG: 29 chars max


def test_render_forward_missing_network():
    r = jdi.render(_fcfg(), {SCRAPE: "br-jess-scrape"}, {})
    assert r.missing == (HOSTACC, "bridge")
    assert "br-jess-hostacc" not in _fwd(r)[0].text


def test_render_forward_off():
    r = jdi.render(_fcfg("off"), FBRIDGES, FSUBNETS)
    assert [rs.family for rs in r.rulesets] == [IPV4, IPV6]
    assert r.absent == (FWD4, FWD6)
    assert r.missing == ()  # forward lines are not looked up while off


def test_render_input_source_check():
    in4 = jdi.render(_cfg(), BRIDGES, {HOSTACC: "10.9.1.0/24", SCRAPE: "10.9.2.0/24"}).rulesets[0]
    main = _main(in4)
    src = [i for i, r in enumerate(main) if "jess: source" in r]
    assert [main[i] for i in src] == [
        f'-A {V4_CHAIN} -i br-jess-hostacc ! -s 10.9.1.0/24 -m comment --comment "jess: source" -j {jdi.IN_SPOOF}',
        f'-A {V4_CHAIN} -i br-jess-scrape ! -s 10.9.2.0/24 -m comment --comment "jess: source" -j {jdi.IN_SPOOF}',
    ]
    assert src == [1, 2], "right after the reply rule, before INVALID and the allow lines"
    spoof = [r for r in _rules(in4) if r.startswith(f"-A {jdi.IN_SPOOF} ")]
    assert spoof[0] == f'-A {jdi.IN_SPOOF} -m comment --comment "jess: spoofed"' and "-j DROP" not in spoof[-1]
    e4 = jdi.render(_cfg("enforce"), BRIDGES, {HOSTACC: "10.9.1.0/24"}).rulesets[0]
    assert [r for r in _rules(e4) if r.startswith(f"-A {jdi.IN_SPOOF} ")][-1] == f"-A {jdi.IN_SPOOF} -j DROP"
    assert (
        jdi.render(_cfg(), BRIDGES, {HOSTACC: "10.9.1.0/24"}).rulesets[1].text
        == jdi.render(_cfg(), BRIDGES).rulesets[1].text
    )  # IPv6 has no source check (and no allow lines)


def test_digest_covers_subchains():
    a = jdi.render(_cfg(), BRIDGES, {HOSTACC: "10.9.1.0/24"}).rulesets[0].digest
    b = jdi.render(_cfg(), BRIDGES, {HOSTACC: "10.9.3.0/24"}).rulesets[0].digest
    c = jdi.render(_cfg("enforce"), BRIDGES, {HOSTACC: "10.9.1.0/24"}).rulesets[0].digest
    assert len({a, b, c}) == 3


def test_docker_networks_subnets(host):
    host.add_network("two", subnets=["10.8.1.0/24", "10.8.2.0/24"])
    host.add_network("dual", subnets=["10.8.3.0/24", "fd00:1::/64"], ipv6=False)
    host.add_network("none-v4", subnets=[])
    host.add_network("bad", subnets=["10.8.4.1/24"])  # host bits: not what Docker prints
    host.add_network("lan", driver="macvlan")
    nets = jdi.docker_networks()
    assert nets.subnets["bridge"] == "172.17.0.0/16"
    assert nets.subnets[HOSTACC] == _subnet(HOSTACC)
    assert nets.subnets["dual"] == "10.8.3.0/24"  # the IPv6 subnet is ignored
    assert sorted(nets.uncovered) == ["bad", "lan", "none-v4", "two"]
    assert "two" not in nets.subnets and "two" in nets.bridges


def test_docker_networks_driver_listing_fails(host):
    host.fail["docker drivers"] = (1, "", "Cannot connect to the Docker daemon")
    with pytest.raises(jdi.DockerUnavailable):
        jdi.docker_networks()


def test_ensure_forward_jumps_on_top(host):
    for rs in _fwd(jdi.render(_fcfg(), FBRIDGES, FSUBNETS)):
        jdi.load_chain(rs)
    for fam, hook in (("ipv4", FWD4), ("ipv6", FWD6)):
        assert jdi.ensure_jumps(hook) is True
        assert host.top2(fam, "FORWARD") == _ours(fam, hook)
        assert host.forward[fam][2:] == DOCKER_FWD
        assert jdi.ensure_jumps(hook) is False
        # tailscaled restart: ts-forward (-o tailscale0 -j ACCEPT) back at FORWARD 1
        host.forward[fam].remove(TS_FWD)
        host.forward[fam].insert(0, TS_FWD)
        assert jdi.ensure_jumps(hook) is True and host.top2(fam, "FORWARD") == _ours(fam, hook)
        # dockerd restart: it puts DOCKER-USER back on top
        host.forward[fam].insert(0, ("-j", "DOCKER-USER"))
        assert jdi.ensure_jumps(hook) is True and host.top2(fam, "FORWARD") == _ours(fam, hook)
        assert host.input[fam] == [TS_JUMP], "FORWARD jumps never touch INPUT"


def test_sync_forward_observe(host, fconf, mdir, capsys):
    w = jdi.Watcher(fconf, mdir)
    w.sync()
    assert host.restored() == ["ipv4", "ipv6", "ipv4", "ipv6"]
    for fam, hook in (("ipv4", FWD4), ("ipv6", FWD6)):
        assert host.top2(fam, "FORWARD") == _ours(fam, hook)
        assert host.forward[fam][2:] == DOCKER_FWD, "Docker's and tailscaled's FORWARD rules stay, in order"
    for chain in (FWD4.chain, jdi.FWD_LAN, jdi.FWD_SPOOF):
        assert host.chains["ipv4"][chain]
    assert "--ctorigdst 10.0.0.1 --ctorigdstport 53" in "\n".join(host.chains["ipv4"][jdi.FWD_LAN])
    err = capsys.readouterr().err
    assert "applied ipv4+ipv6+ipv4-fwd+ipv6-fwd mode=observe allow_rules=" in err
    assert "forward-mode=observe forward_rules=" in err
    m = _metrics(mdir)
    assert m["jess_docker_input_forward_active"] == "1" and m["jess_docker_input_forward_enforcing"] == "0"
    assert m['jess_docker_input_forward_unmatched_packets_total{family="ipv4"}'] == "0"
    assert m['jess_docker_input_spoofed_packets_total{path="forward"}'] == "0"
    assert set(jdi.METRICS) == {k.split("{")[0] for k in m}
    n = len(host.writes())
    w.sync()
    assert len(host.writes()) == n, "healthy FORWARD chains must not be reloaded"


def test_sync_forward_repairs_without_reload(host, fconf, mdir, capsys):
    w = jdi.Watcher(fconf, mdir)
    w.sync()
    host.counters["ipv4"][jdi.FWD_UNMATCHED_COMMENT] = 7
    host.forward["ipv4"].remove(TS_FWD)
    host.forward["ipv4"].insert(0, TS_FWD)  # tailscaled restarted
    capsys.readouterr()
    w.sync()
    assert host.restored() == ["ipv4", "ipv6", "ipv4", "ipv6"], "a jump repair must not reload a chain"
    assert host.top2("ipv4", "FORWARD") == _ours("ipv4", FWD4)
    assert "ipv4-fwd: jumps placed at the top of FORWARD" in capsys.readouterr().err
    w.metrics()
    assert _metrics(mdir)['jess_docker_input_forward_unmatched_packets_total{family="ipv4"}'] == "7"


def test_sync_forward_subchain_drift_reloads(host, fconf, mdir):
    w = jdi.Watcher(fconf, mdir)
    w.sync()
    lan = host.chains["ipv4"][jdi.FWD_LAN]
    lan[1] = lan[1].replace("--ctorigdstport 53", "--ctorigdstport 1:65535")  # replaced in place, same count
    w.sync()
    assert host.restored()[4:] == ["ipv4"]
    assert "1:65535" not in "\n".join(host.chains["ipv4"][jdi.FWD_LAN])
    del host.chains["ipv4"][jdi.FWD_SPOOF][-1]
    w.sync()
    assert host.restored()[5:] == ["ipv4"]


def test_sync_forward_mode_changes(host, fconf, mdir, capsys):
    w = jdi.Watcher(fconf, mdir)
    w.sync()
    fconf.write_text(fconf.read_text().replace("forward-mode observe", "forward-mode enforce"))
    w.sync()
    assert host.chains["ipv4"][jdi.FWD_LAN][-1].endswith("-j REJECT --reject-with icmp-admin-prohibited")
    assert host.chains["ipv4"][V4_CHAIN][-1] != f"-A {V4_CHAIN} -j DROP", "INPUT stays in observe"
    assert _metrics(mdir)["jess_docker_input_forward_enforcing"] == "1"
    input_before = copy.deepcopy((host.input, host.chains["ipv4"][V4_CHAIN], host.chains["ipv6"][V6_CHAIN]))
    fconf.write_text(fconf.read_text().replace("forward-mode enforce", "forward-mode off"))
    capsys.readouterr()
    w.sync()
    for fam, hook in (("ipv4", FWD4), ("ipv6", FWD6)):
        assert host.forward[fam] == DOCKER_FWD
        assert not set(hook.chains) & set(host.chains[fam])
    assert set(FOREIGN_CHAINS) <= set(host.chains["ipv4"])
    assert (host.input, host.chains["ipv4"][V4_CHAIN], host.chains["ipv6"][V6_CHAIN]) == input_before
    err = capsys.readouterr().err
    assert "ipv4-fwd: chains and jumps removed (forward-mode off)" in err
    m = _metrics(mdir)
    assert m["jess_docker_input_forward_active"] == "0"
    assert "jess_docker_input_forward_unmatched_packets_total" not in "".join(m)
    n = len(host.writes())
    w.sync()
    assert len(host.writes()) == n, "nothing to remove the second time"
    fconf.write_text(fconf.read_text().replace("forward-mode off", "forward-mode observe"))
    w.sync()
    assert host.top2("ipv4", "FORWARD") == _ours("ipv4", FWD4)


def test_sync_forward_bad_line_keeps_everything(host, fconf, mdir, capsys):
    w = jdi.Watcher(fconf, mdir)
    w.sync()
    before = host.state()
    fconf.write_text(fconf.read_text() + "forward n 10.0.0.1 tcp 80\n")
    capsys.readouterr()
    w.sync()
    assert host.state() == before
    assert "the router (10.0.0.1) may only be allowed on port 53" in capsys.readouterr().err
    assert _metrics(mdir)["jess_docker_input_check_ok"] == "0"


def test_main_remove_removes_forward(host, fconf, monkeypatch):
    assert jdi.main(["apply", "--config", str(fconf)]) == 0
    assert host.top2("ipv4", "FORWARD") == _ours("ipv4", FWD4)
    monkeypatch.setenv("SERVICE_RESULT", "success")
    assert jdi.main(["remove", "--only-if-clean-stop"]) == 0
    for fam in FAM:
        assert host.input[fam] == [TS_JUMP] and host.forward[fam] == DOCKER_FWD
        assert set(host.chains[fam]) == set(FOREIGN_CHAINS)


def test_main_apply_with_forward_off_removes_forward(host, fconf, conf):
    assert jdi.main(["apply", "--config", str(fconf)]) == 0
    assert jdi.main(["apply", "--config", str(conf)]) == 0  # the same, forward-mode off
    assert host.forward["ipv4"] == DOCKER_FWD and FWD4.chain not in host.chains["ipv4"]
    assert host.top2("ipv4") == _ours("ipv4")


def test_main_check_with_forward(host, fconf, capsys):
    assert jdi.main(["check", "--config", str(fconf)]) == 0
    (line,) = [x for x in capsys.readouterr().err.splitlines() if "config ok" in x]
    assert re.fullmatch(
        r"jess-docker-input: config ok: mode=observe allow_rules=4 forward-mode=observe forward_rules=\d+ "
        r"ipv4=[0-9a-f]{12} ipv6=[0-9a-f]{12} ipv4-fwd=[0-9a-f]{12} ipv6-fwd=[0-9a-f]{12}",
        line,
    )
    assert host.writes() == []


def test_main_status_lists_forward_chains(host, fconf, capsys):
    assert jdi.main(["apply", "--config", str(fconf)]) == 0
    capsys.readouterr()
    assert jdi.main(["status"]) == 0
    out = capsys.readouterr().out
    for chain in (*IPV4.chains, *IPV6.chains, *FWD4.chains, *FWD6.chains):
        assert f"Chain {chain} (" in out


_FWD_KLOG = [
    pytest.param(
        "jess-docker-fwd observe: IN=br-1f56b9570309 OUT=enp7s0 MAC=aa:bb SRC=172.25.0.3 DST=10.0.0.195 LEN=60 "
        "TOS=0x00 PREC=0x00 TTL=63 ID=1 DF PROTO=TCP SPT=40000 DPT=22 WINDOW=64240 RES=0x00 SYN URGP=0",
        ("jess-docker-fwd", "observe", "br-1f56b9570309", "10.0.0.195", "TCP", "22"),
        id="fwd-tcp",
    ),
    pytest.param(
        "jess-docker-spoof reject: IN=br-jess-hostacc OUT=enp7s0 MAC=aa:bb SRC=10.0.0.77 DST=10.0.0.1 LEN=30 "
        "TOS=0x00 PREC=0x00 TTL=63 ID=1 PROTO=UDP SPT=5351 DPT=5351 LEN=10",
        ("jess-docker-spoof", "reject", "br-jess-hostacc", "10.0.0.1", "UDP", "5351"),
        id="spoof-udp",
    ),
    pytest.param(
        "jess-docker-fwd6 observe: IN=docker0 OUT=enp7s0 MAC=aa:bb SRC=fd00::2 DST=fd56::1 LEN=80 TC=0 "
        "HOPLIMIT=63 FLOWLBL=0 PROTO=TCP SPT=40000 DPT=80 WINDOW=64800 RES=0x00 SYN URGP=0",
        ("jess-docker-fwd6", "observe", "docker0", "fd56::1", "TCP", "80"),
        id="fwd6-tcp",
    ),
]


@pytest.mark.parametrize("line,groups", _FWD_KLOG)
def test_klog_regex_forward(line, groups):
    m = jdi._KLOG_RE.search(line)
    assert m is not None and m.groups() == groups


def test_summary_forward_paths(host):
    host.journal = "\n".join(p.values[0] for p in _FWD_KLOG) + "\n"
    rows = [row.split() for row in jdi.summary(1).splitlines()[1:]]
    assert ["1", "fwd", "observe", "br-1f56b9570309", "10.0.0.195", "TCP", "22"] in rows
    assert ["1", "spoof", "reject", HOSTACC, "10.0.0.1", "UDP", "5351"] in rows
    assert ["1", "fwd6", "observe", "bridge", "fd56::1", "TCP", "80"] in rows
    (journal_call,) = [c for c in host.calls if c[0] == jdi.JOURNALCTL]
    assert journal_call[-2:] == ("-g", "jess-docker-")
