#!/usr/bin/env python3
"""Functional self-test of jess_docker_input.py in throwaway network namespaces.

    sudo python3 -I scripts/container-firewall/selftest_netns.py

Builds a fake Jupiter in namespaces named `jdt-*` (nothing is created in the
host's own namespace, and every one is deleted at the end, also on failure):

    jdt-ctr  (netx 192.168.250.2) --+                       +-- jdt-lan: "Helios" 10.0.0.195,
                                    +-- br-jdtx  jdt-host --+   "router" 10.0.0.1, "internet" 198.51.100.1
    jdt-dual (netx .250.3, nety     |   (10.0.0.248, Docker-   +-- jdt-tail: tailnet peer 100.100.0.2
              .251.2 = default) ----+-- br-jdty  like NAT/FORWARD/DNAT)

jdt-dual is a container on two networks with ip_forward=1, as on Jupiter. In
jdt-host the real watcher (Watcher.sync, with Docker's network list replaced by
the two bridges above) loads the chains, then this script checks, with real
connections: observe passes everything and counts it; in enforce only the
allowed (network, destination, port) triples get through, DNAT'd published
ports match by the address asked for, LAN clients of a published port still
work, the internet passes, the tailnet is blocked, a packet routed through the
two-network container or with a forged source is dropped (both paths), a
tailscaled-style `ts-forward` jump inserted on top is undone by the next sync,
and the rollbacks (`forward-mode off`, `remove`) leave the tables exactly as
they were. Kernel LOG output from non-host namespaces is off by default, so the
log lines are not tested here (the unit tests cover them).

Exit 0 = every check passed. Stdlib only, needs root, iproute2 and iptables-nft.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "jess_docker_input.py"
NS = ("jdt-host", "jdt-ctr", "jdt-dual", "jdt-lan", "jdt-tail")
NETX, NETY = "192.168.250.0/24", "192.168.251.0/24"

SERVER = r"""
import socket, sys, threading
def tcp(addr, port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((addr, port)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
def udp(addr, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind((addr, port))
    while True:
        d, a = s.recvfrom(512); s.sendto(d, a)
for spec in sys.argv[1:]:
    proto, addr, port = spec.split(":")
    threading.Thread(target=tcp if proto == "tcp" else udp, args=(addr, int(port)), daemon=True).start()
threading.Event().wait()
"""

CLIENT = r"""
import errno, socket, sys
proto, host, port, src = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM)
s.settimeout(1.5)
if src != "-":
    s.bind((src, 0))
try:
    s.connect((host, port))
    if proto == "udp":
        s.send(b"x"); s.recv(16)
    print("open")
except TimeoutError:
    print("timeout")
except ConnectionRefusedError:
    print("refused")
except OSError as e:
    print("blocked" if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH) else f"err{e.errno}")
"""

# Runs inside jdt-host: the real watcher, with Docker's answer replaced.
INNER = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("jdi", sys.argv[1]); jdi = importlib.util.module_from_spec(spec)
sys.modules["jdi"] = jdi; spec.loader.exec_module(jdi)
nets = jdi.Networks({"netx": "br-jdtx", "nety": "br-jdty"}, [], {"netx": sys.argv[4], "nety": sys.argv[5]})
jdi.docker_networks = lambda: nets
jdi.default_gateway = lambda: "10.0.0.1"
if sys.argv[2] == "remove":
    for hook in jdi.HOOKS:
        jdi.remove(hook)
    print(json.dumps({"ok": True}))
    sys.exit()
w = jdi.Watcher(Path(sys.argv[2]), Path(sys.argv[3]))
w.sync()
print(json.dumps({"ok": w.last_ok, "problem": w.last_problem}))
"""

CONFIG = """mode {mode}
forward-mode {fmode}
allow nety tcp 8123
forward nety 10.0.0.195 tcp 8080
forward netx 10.0.0.1 udp 53
forward netx 10.0.0.248 tcp 8777
"""

results: list[tuple[bool, str]] = []
servers: list[subprocess.Popen] = []


def sh(*cmd: str, ns: str | None = None, check: bool = True, input_text: str | None = None) -> str:
    full = ["ip", "netns", "exec", ns, *cmd] if ns else list(cmd)
    res = subprocess.run(full, capture_output=True, text=True, input=input_text, timeout=60)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(full)}: {res.stderr.strip()}")
    return res.stdout


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(("PASS " if ok else "FAIL ") + what, flush=True)


def conn(ns: str, proto: str, host: str, port: int, src: str = "-") -> str:
    return sh("python3", "-I", "-c", CLIENT, proto, host, str(port), src, ns=ns).strip()


def expect(ns: str, proto: str, host: str, port: int, want: str, what: str, src: str = "-") -> None:
    got = conn(ns, proto, host, port, src)
    check(got == want, f"{what}: {ns} -> {host}:{port}/{proto} = {got} (want {want})")


def ipt(*args: str, v6: bool = False) -> str:
    return sh("ip6tables" if v6 else "iptables", "-w", *args, ns="jdt-host")


def filter_rules() -> str:
    """The host namespace's filter table, without counters (iptables -S)."""
    return ipt("-S") + ipt("-S", v6=True)


def counter(chain: str, comment: str) -> int:
    for line in ipt("-L", chain, "-n", "-v", "-x").splitlines():
        if f"/* {comment} */" in line:
            return int(line.split()[0])
    return -1


def sync(tmp: Path, mode: str, fmode: str) -> None:
    conf = tmp / "jess-docker-input.conf"
    conf.write_text(CONFIG.format(mode=mode, fmode=fmode))
    out = json.loads(sh("python3", "-I", "-c", INNER, str(SCRIPT), str(conf), str(tmp), NETX, NETY, ns="jdt-host"))
    check(out["ok"], f"sync mode={mode} forward-mode={fmode} ok ({out.get('problem') or 'no problem'})")


def remove_all(tmp: Path) -> None:
    sh("python3", "-I", "-c", INNER, str(SCRIPT), "remove", str(tmp), NETX, NETY, ns="jdt-host")


def setup() -> None:
    for ns in NS:
        sh("ip", "netns", "add", ns)
        sh("ip", "link", "set", "lo", "up", ns=ns)
    h = "jdt-host"
    for br, addr in (("br-jdtx", "192.168.250.1/24"), ("br-jdty", "192.168.251.1/24")):
        sh("ip", "link", "add", br, "type", "bridge", ns=h)
        sh("ip", "addr", "add", addr, "dev", br, ns=h)
        sh("ip", "link", "set", br, "up", ns=h)

    def veth(host_if: str, peer_ns: str, peer_if: str, master: str | None = None) -> None:
        sh("ip", "link", "add", host_if, "type", "veth", "peer", "name", peer_if, "netns", peer_ns, ns=h)
        if master:
            sh("ip", "link", "set", host_if, "master", master, ns=h)
        sh("ip", "link", "set", host_if, "up", ns=h)
        sh("ip", "link", "set", peer_if, "up", ns=peer_ns)

    veth("jdtv-ctr", "jdt-ctr", "eth0", "br-jdtx")
    sh("ip", "addr", "add", "192.168.250.2/24", "dev", "eth0", ns="jdt-ctr")
    sh("ip", "route", "add", "default", "via", "192.168.250.1", ns="jdt-ctr")
    veth("jdtv-dualy", "jdt-dual", "eth0", "br-jdty")
    veth("jdtv-dualx", "jdt-dual", "eth1", "br-jdtx")
    sh("ip", "addr", "add", "192.168.251.2/24", "dev", "eth0", ns="jdt-dual")
    sh("ip", "addr", "add", "192.168.250.3/24", "dev", "eth1", ns="jdt-dual")
    sh("ip", "route", "add", "default", "via", "192.168.251.1", ns="jdt-dual")
    for key, value in (("net.ipv4.ip_forward", "1"), ("net.ipv4.conf.all.rp_filter", "2")):
        sh("sysctl", "-qw", f"{key}={value}", ns="jdt-dual")  # what Jupiter's containers inherit
    veth("jdtlan0", "jdt-lan", "eth0")
    sh("ip", "addr", "add", "10.0.0.248/24", "dev", "jdtlan0", ns=h)
    for addr in ("10.0.0.195/24", "10.0.0.1/24"):
        sh("ip", "addr", "add", addr, "dev", "eth0", ns="jdt-lan")
    sh("ip", "addr", "add", "198.51.100.1/32", "dev", "lo", ns="jdt-lan")
    sh("ip", "route", "add", "198.51.100.1/32", "via", "10.0.0.195", ns=h)
    veth("jdttail0", "jdt-tail", "eth0")
    sh("ip", "addr", "add", "100.100.0.1/30", "dev", "jdttail0", ns=h)
    sh("ip", "addr", "add", "100.100.0.2/30", "dev", "eth0", ns="jdt-tail")
    sh("ip", "route", "add", "default", "via", "100.100.0.1", ns="jdt-tail")
    sh("sysctl", "-qw", "net.ipv4.ip_forward=1", ns=h)
    sh("sysctl", "-qw", "net.ipv4.conf.all.rp_filter=2", ns=h)
    # Docker-like NAT + FORWARD, plus a tailscaled-like ts-forward below them.
    rules = """*nat
-A PREROUTING -d 10.0.0.248/32 ! -i br-jdty -p tcp --dport 8777 -j DNAT --to-destination 192.168.251.2:8000
-A PREROUTING -d 10.0.0.248/32 ! -i br-jdty -p tcp --dport 8778 -j DNAT --to-destination 192.168.251.2:8000
-A POSTROUTING -s 192.168.250.0/24 ! -o br-jdtx -j MASQUERADE
-A POSTROUTING -s 192.168.251.0/24 ! -o br-jdty -j MASQUERADE
COMMIT
*filter
:FORWARD DROP [0:0]
:DOCKER-USER - [0:0]
:ts-forward - [0:0]
-A FORWARD -j DOCKER-USER
-A FORWARD -o br-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
-A FORWARD -d 192.168.251.2/32 ! -i br-jdty -o br-jdty -p tcp --dport 8000 -j ACCEPT
-A FORWARD -i br-+ -j ACCEPT
-A FORWARD -j ts-forward
-A ts-forward -o jdttail0 -j ACCEPT
-A ts-forward -i jdttail0 -j ACCEPT
COMMIT
"""
    sh("iptables-restore", "-w", ns=h, input_text=rules)
    for ns, specs in (
        ("jdt-lan", ["tcp:10.0.0.195:8080", "tcp:10.0.0.195:22", "tcp:10.0.0.1:80", "udp:10.0.0.1:53",
                     "tcp:198.51.100.1:443"]),
        ("jdt-tail", ["tcp:100.100.0.2:22"]),
        ("jdt-dual", ["tcp:0.0.0.0:8000"]),
        ("jdt-host", ["tcp:10.0.0.248:8123"]),
    ):  # fmt: skip
        servers.append(subprocess.Popen(["ip", "netns", "exec", ns, "python3", "-I", "-c", SERVER, *specs]))
    time.sleep(1.0)


def teardown() -> None:
    for p in servers:
        p.kill()
        p.wait()
    for ns in NS:
        subprocess.run(["ip", "netns", "del", ns], capture_output=True)


def scenarios(tmp: Path) -> None:
    before, fwd_before = filter_rules(), ipt("-S", "FORWARD")
    expect("jdt-ctr", "tcp", "10.0.0.195", 8080, "open", "no filter yet")

    # ---- observe: nothing is blocked, everything unmatched is counted
    sync(tmp, "observe", "observe")
    check(ipt("-S", "FORWARD").splitlines()[1:3] == [
        "-A FORWARD -i br-+ -m comment --comment jess-docker-input -j JESS-DOCKER-FWD",
        "-A FORWARD -i docker0 -m comment --comment jess-docker-input -j JESS-DOCKER-FWD",
    ], "FORWARD jumps at positions 1-2")  # fmt: skip
    check("JESS-DOCKER-FWD6" in ipt("-S", "FORWARD", v6=True), "IPv6 FORWARD jump present")
    expect("jdt-ctr", "tcp", "10.0.0.195", 22, "open", "observe passes a LAN port nobody allowed")
    expect("jdt-ctr", "tcp", "100.100.0.2", 22, "open", "observe passes the tailnet")
    check(counter("JESS-DOCKER-FWD-LAN", "jess: fwd unmatched") >= 2, "observe counts unmatched FORWARD packets")
    sh("ip", "route", "add", "10.0.0.195/32", "via", "192.168.250.3", ns="jdt-ctr")  # borrow jdt-dual's network
    spoof0 = counter("JESS-DOCKER-FWD-SPOOF", "jess: spoofed")
    expect("jdt-ctr", "tcp", "10.0.0.195", 8080, "open", "observe passes a borrowed route")
    check(counter("JESS-DOCKER-FWD-SPOOF", "jess: spoofed") > spoof0, "... and counts it as spoofed")
    sh("ip", "route", "del", "10.0.0.195/32", ns="jdt-ctr")

    # ---- enforce
    sync(tmp, "enforce", "enforce")
    expect("jdt-dual", "tcp", "10.0.0.195", 8080, "open", "allowed (nety, Helios, 8080)")
    expect("jdt-dual", "tcp", "10.0.0.195", 22, "blocked", "same network, port not allowed")
    expect("jdt-ctr", "tcp", "10.0.0.195", 8080, "blocked", "allowed for nety, not netx")
    expect("jdt-ctr", "udp", "10.0.0.1", 53, "open", "router DNS allowed for netx")
    time.sleep(1.1)  # past the ICMP rate limit for this source
    expect("jdt-ctr", "tcp", "10.0.0.1", 80, "blocked", "router admin page blocked")
    expect("jdt-ctr", "tcp", "10.0.0.248", 8777, "open", "published port allowed by the address asked for (DNAT)")
    time.sleep(1.1)
    expect("jdt-ctr", "tcp", "10.0.0.248", 8778, "blocked", "another published port blocked")
    expect("jdt-lan", "tcp", "10.0.0.248", 8777, "open", "a LAN client of a published port still works")
    expect("jdt-ctr", "tcp", "198.51.100.1", 443, "open", "the internet passes")
    time.sleep(1.1)
    expect("jdt-ctr", "tcp", "100.100.0.2", 22, "blocked", "tailnet blocked")
    # tailscaled restart: ts-forward back on top accepts everything to the tailnet ...
    ipt("-I", "FORWARD", "1", "-j", "ts-forward")
    expect("jdt-ctr", "tcp", "100.100.0.2", 22, "open", "(bypass while ts-forward is on top)")
    sync(tmp, "enforce", "enforce")  # ... until the next sync puts our jumps back above it
    check(ipt("-S", "FORWARD").splitlines()[1].endswith("-j JESS-DOCKER-FWD"), "sync moved our jumps back on top")
    time.sleep(1.1)
    expect("jdt-ctr", "tcp", "100.100.0.2", 22, "blocked", "tailnet blocked again after the repair")
    ipt("-D", "FORWARD", "-j", "ts-forward")  # drop the simulated restart's extra jump (the first match)
    # borrowing a two-network container's route, to the LAN and to the host
    sh("ip", "route", "add", "10.0.0.195/32", "via", "192.168.250.3", ns="jdt-ctr")
    expect("jdt-ctr", "tcp", "10.0.0.195", 8080, "timeout", "FORWARD: borrowed route dropped")
    sh("ip", "route", "del", "10.0.0.195/32", ns="jdt-ctr")
    expect("jdt-dual", "tcp", "10.0.0.248", 8123, "open", "INPUT allow line (nety -> host 8123)")
    expect("jdt-ctr", "tcp", "10.0.0.248", 8123, "refused", "INPUT: netx not allowed (tcp-reset)")
    sh("ip", "route", "add", "10.0.0.248/32", "via", "192.168.250.3", ns="jdt-ctr")
    in_spoof0 = counter("JESS-DOCKER-IN-SPOOF", "jess: spoofed")
    expect("jdt-ctr", "tcp", "10.0.0.248", 8123, "timeout", "INPUT: borrowed route dropped")
    check(counter("JESS-DOCKER-IN-SPOOF", "jess: spoofed") > in_spoof0, "... and counted on INPUT")
    sh("ip", "route", "del", "10.0.0.248/32", ns="jdt-ctr")
    # forged LAN source address
    sh("ip", "addr", "add", "10.0.0.77/32", "dev", "eth0", ns="jdt-ctr")
    fwd_spoof0 = counter("JESS-DOCKER-FWD-SPOOF", "jess: spoofed")
    expect("jdt-ctr", "udp", "10.0.0.1", 53, "timeout", "forged source dropped", src="10.0.0.77")
    check(counter("JESS-DOCKER-FWD-SPOOF", "jess: spoofed") > fwd_spoof0, "... and counted on FORWARD")
    sh("ip", "addr", "del", "10.0.0.77/32", "dev", "eth0", ns="jdt-ctr")

    # ---- rollback 1: forward-mode off removes only the FORWARD side
    input_before = ipt("-S", "INPUT") + ipt("-S", "JESS-DOCKER-IN")
    sync(tmp, "enforce", "off")
    fwd_now = ipt("-S")
    check("JESS-DOCKER-FWD" not in fwd_now and "JESS-DOCKER-FWD6" not in ipt("-S", v6=True), "forward-mode off removed")
    check(ipt("-S", "INPUT") + ipt("-S", "JESS-DOCKER-IN") == input_before, "... and left INPUT as it was")
    check(ipt("-S", "FORWARD") == fwd_before, "FORWARD is exactly as before the install")
    time.sleep(1.1)
    expect("jdt-ctr", "tcp", "10.0.0.195", 22, "open", "LAN reachable again (as before the install)")

    # ---- rollback 2: remove = exactly the tables from before the first sync
    remove_all(tmp)
    check(filter_rules() == before, "remove restored the filter tables exactly")


def main() -> int:
    if os.geteuid() != 0:
        print("needs root: sudo python3 -I scripts/container-firewall/selftest_netns.py", file=sys.stderr)
        return 2
    leftovers = [
        ns for ns in NS if ns in subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout
    ]
    if leftovers:
        print(f"namespaces left from an earlier run, delete them first: {leftovers}", file=sys.stderr)
        return 2
    try:
        setup()
        with tempfile.TemporaryDirectory(prefix="jdt-") as tmp:
            scenarios(Path(tmp))
    except Exception as e:  # report, then clean up
        check(False, f"aborted: {type(e).__name__}: {e}")
    finally:
        teardown()
    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
