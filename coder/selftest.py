"""Coder box isolation self-test. Runs INSIDE a box; exit 0 only if every check passes.

scripts/jess-coder runs it at the start of every session (in the same container,
before Qwen Code starts) and on `jess-coder selftest`; the CI deploy runs it
after building the image. A failure means the box must not open.

What it proves, from inside:

* identity: not root, the box uid, no capabilities, no_new_privs, seccomp on;
* filesystem: root mount read-only, writable mounts only where the launcher puts
  them, no Docker socket;
* network: only lo + eth0, an address in the box range, no default route, IPv6
  off (no IPv6 address but ::1, eth0's disable_ipv6 set);
* reach: external DNS fails; every host address the launcher passed (Jupiter's
  own addresses incl. every Docker bridge gateway, Helios, the LAN router...),
  public resolvers and IPv6 (incl. the bridge's link-local) answer a TCP connect
  with "no route" at once. That is an any-port proof: a refusal (RST) or a
  timeout means packets got somewhere, and counts as reach. The gate host may
  refuse; the bridge's .1 may only be absent. UDP DNS gets no answer;
* gate: the orchestrator answers 403 on any path but the two gate paths, 401
  without a valid coder token, and (with one) 200 on /models.

Inputs (environment):
  JESS_SELFTEST_HOSTS   comma-separated IPv4 addresses to try (required; the launcher's list)
  JESS_SELFTEST_HOSTS_V6  the coder bridge's fe80:: addresses (probed via eth0)
  JESS_CODER_SUBNET     coder-net's CIDR (its first address is probed too)
  JESS_CODER_BOX_RANGE  CIDR the box's own address must be in
  JESS_CODER_UID        expected uid (default 61001)
  JESS_BRAIN_KEY        a coder token; adds the "token works" check
  JESS_SELFTEST_EXPECT_RW  comma-separated mount points allowed to be writable
The gate URL comes from /etc/qwen-code/settings.json (baseUrl), the file Qwen
Code itself uses, so the test can't check one address while Qwen talks to another.

Output: one line per check, no content beyond addresses, ports and status codes.
Stdlib only (runs with python3 -I -S).
"""

from __future__ import annotations

import concurrent.futures as cf
import errno
import http.client
import ipaddress
import json
import os
import socket
import struct
import sys
import time
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

SETTINGS = "/etc/qwen-code/settings.json"
GATE_PREFIX = "/api/coder/v1"

# Ports any Jupiter/LAN service listens on (or might): SSH, DNS, HTTP(S),
# Grafana, dashboard, Actual, Radicale, ESPHome, TTS, llama, HA, orchestrator,
# Prometheus, node-exporter, Netdata, SMB.
PROBE_PORTS = (22, 53, 80, 443, 445, 3000, 3001, 3100, 5006, 5232, 6053, 8004, 8080, 8123, 8888, 9090, 9100, 19999)
PUBLIC_V4 = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
PUBLIC_V6 = ("2606:4700:4700::1111", "2001:4860:4860::8888")
DNS_NAMES = ("example.com", "github.com", "registry.npmjs.org", "pypi.org")
CONNECT_TIMEOUT_S = 4.0
DNS_TIMEOUT_S = 15.0
MAX_HOSTS = 256
# Connect errors that prove "no route at all" (so no port on that address).
NO_ROUTE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT}
DEFAULT_RW = ("/work", "/tmp", "/home/coder", "/app/data")
# Kernel/runtime mounts Docker always makes writable for the container.
RUNTIME_RW_PREFIXES = ("/proc", "/dev", "/sys/fs/cgroup")


class Results:
    def __init__(self) -> None:
        self.failed: List[str] = []
        self.count = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.count += 1
        if ok:
            print(f"ok   {name}")
        else:
            self.failed.append(name)
            print(f"FAIL {name}" + (f": {detail}" if detail else ""))
        sys.stdout.flush()
        return ok


# ------------------------------------------------------------------ identity


def _status_fields() -> dict:
    out = {}
    with open("/proc/self/status") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            out[key] = value.strip()
    return out


def check_identity(res: Results) -> None:
    want_uid = int(os.environ.get("JESS_CODER_UID", "61001"))
    res.check("runs as the box uid", os.getuid() == want_uid and os.geteuid() == want_uid, f"uid {os.getuid()}")
    res.check("not root", os.getuid() != 0 and os.getgid() != 0 and 0 not in os.getgroups())
    st = _status_fields()
    caps = {k: st.get(k, "?") for k in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")}
    res.check(
        "no capabilities", all(v.strip("0") == "" for v in caps.values()), " ".join(f"{k}={v}" for k, v in caps.items())
    )
    res.check("no_new_privs set", st.get("NoNewPrivs") == "1", f"NoNewPrivs={st.get('NoNewPrivs')}")
    res.check("seccomp filter on", st.get("Seccomp") == "2", f"Seccomp={st.get('Seccomp')}")


# ---------------------------------------------------------------- filesystem


def _mounts() -> List[Tuple[str, str, str]]:
    """(mount point, options, fstype) from /proc/self/mountinfo."""
    out = []
    with open("/proc/self/mountinfo") as fh:
        for line in fh:
            left, _, right = line.partition(" - ")
            fields = left.split()
            point = fields[4].encode().decode("unicode_escape")
            opts = fields[5]
            fstype = right.split()[0] if right else "?"
            out.append((point, opts, fstype))
    return out


def check_filesystem(res: Results) -> None:
    mounts = _mounts()
    root = [m for m in mounts if m[0] == "/"]
    res.check(
        "root filesystem read-only", bool(root) and "ro" in root[-1][1].split(","), root[-1][1] if root else "no /"
    )
    allowed = tuple(p for p in os.environ.get("JESS_SELFTEST_EXPECT_RW", ",".join(DEFAULT_RW)).split(",") if p)
    bad = []
    for point, opts, _fstype in mounts:
        if "rw" not in opts.split(",") or point == "/":
            continue
        if point in allowed or any(point == p or point.startswith(p + "/") for p in RUNTIME_RW_PREFIXES):
            continue
        bad.append(point)
    res.check("writable only where expected", not bad, ", ".join(sorted(bad)[:8]))
    socks = [
        p for p in ("/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock") if os.path.exists(p)
    ]
    res.check("no Docker or containerd socket", not socks, ", ".join(socks))


# ------------------------------------------------------------------- network


def _interfaces() -> List[str]:
    with open("/proc/net/dev") as fh:
        return [line.split(":", 1)[0].strip() for line in fh.readlines()[2:] if ":" in line]


def _has_default_route_v4() -> bool:
    with open("/proc/net/route") as fh:
        for line in fh.readlines()[1:]:
            parts = line.split()
            if len(parts) > 7 and parts[1] == "00000000" and parts[7] == "00000000":
                return True
    return False


def _v6_addresses() -> List[ipaddress.IPv6Address]:
    try:
        with open("/proc/net/if_inet6") as fh:
            return [ipaddress.IPv6Address(int(line.split()[0], 16)) for line in fh if line.strip()]
    except FileNotFoundError:  # IPv6 off in this kernel/netns
        return []


def _own_ipv4(towards: str) -> Optional[str]:
    """Source address the kernel picks towards ``towards`` (UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((towards, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def check_network_config(res: Results, gate_host: str) -> None:
    ifaces = _interfaces()
    res.check("interfaces are lo + eth0 only", sorted(ifaces) == ["eth0", "lo"], ", ".join(sorted(ifaces)))
    res.check("no IPv4 default route", not _has_default_route_v4())
    v6 = [a for a in _v6_addresses() if not a.is_loopback]
    res.check("no IPv6 address but ::1", not v6, ", ".join(str(a) for a in v6[:4]))
    try:
        with open("/proc/sys/net/ipv6/conf/eth0/disable_ipv6") as fh:
            off = fh.read().strip() == "1"
    except FileNotFoundError:  # IPv6 not in this kernel/netns at all
        off = True
    res.check("IPv6 disabled on eth0", off)
    own = _own_ipv4(gate_host)
    box_range = os.environ.get("JESS_CODER_BOX_RANGE", "")
    if box_range:
        try:
            inside = own is not None and ipaddress.IPv4Address(own) in ipaddress.IPv4Network(box_range, strict=False)
        except ValueError:
            inside = False
        res.check("own address in the box range", inside, f"{own} not in {box_range}")


# ---------------------------------------------------------------------- reach


def _tcp_reach(target: Tuple[str, int, int, str]) -> Optional[str]:
    """None if ``target`` behaves as expected, else what happened.

    expect = "no_route" (off-subnet: only an immediate no-route error is fine),
    "closed" (the gate host: a refusal is fine too) or "absent" (an empty
    in-subnet address: no-route or a timeout, never an answer).
    """
    host, port, family, expect = target
    where = f"[{host}]:{port}" if family == socket.AF_INET6 else f"{host}:{port}"
    try:
        if "%" in host:  # link-local: the scope must be numeric, "%eth0" alone fails with EINVAL
            addr, iface = host.split("%", 1)
            sockaddr: tuple = (addr, port, 0, socket.if_nametoindex(iface))
        else:
            sockaddr = (host, port)
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(CONNECT_TIMEOUT_S)
            s.connect(sockaddr)
    except TimeoutError:
        return None if expect == "absent" else f"{where} timed out (packets left the box)"
    except OSError as exc:
        if exc.errno in NO_ROUTE:
            return None
        if exc.errno == errno.ECONNREFUSED and expect == "closed":
            return None
        return f"{where} {errno.errorcode.get(exc.errno, exc.errno)}"
    return f"{where} connected"


def _tcp_connects(target: Tuple[str, int, int]) -> Optional[str]:
    """Strict probe: anything but no-route is a hit (kept for tests and callers)."""
    return _tcp_reach((*target, "no_route"))


def _dns_query(name: str) -> bytes:
    header = struct.pack(">HHHHHH", 0x4A53, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return header + qname + struct.pack(">HH", 1, 1)


def _udp_dns_answers(host: str) -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2.0)
            s.sendto(_dns_query("example.com"), (host, 53))
            s.recvfrom(512)
    except OSError:  # no route, or sent and nothing came back
        return None
    return f"udp {host}:53"


def _resolves(name: str) -> Optional[str]:
    try:
        socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    return name


def _parse_hosts(raw: str) -> List[str]:
    out = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            addr = ipaddress.IPv4Address(item)
        except ValueError:
            continue
        if not addr.is_loopback and str(addr) not in out:
            out.append(str(addr))
    return out


def _parse_link_local(raw: str) -> List[str]:
    out = []
    for item in raw.split(","):
        try:
            addr = ipaddress.IPv6Address(item.strip())
        except ValueError:
            continue
        if addr.is_link_local:
            out.append(f"{addr}%eth0")
    return out


def _run_all(fn: Callable, items: Iterable, timeout: float) -> Tuple[List[str], int]:
    """Run ``fn`` over ``items`` in threads. Returns (hits, how many never finished)."""
    items = list(items)
    hits: List[str] = []
    pool = cf.ThreadPoolExecutor(max_workers=64)
    futures = [pool.submit(fn, i) for i in items]
    done, pending = cf.wait(futures, timeout=timeout)
    for f in done:
        hit = f.result()
        if hit:
            hits.append(hit)
    pool.shutdown(wait=False, cancel_futures=True)
    return sorted(hits), len(pending)


def check_reach(res: Results, gate_host: str, gate_port: int) -> None:
    hosts = _parse_hosts(os.environ.get("JESS_SELFTEST_HOSTS", ""))
    res.check("launcher passed host addresses to try", bool(hosts), "JESS_SELFTEST_HOSTS is empty")
    res.check(f"at most {MAX_HOSTS} host addresses", len(hosts) <= MAX_HOSTS, f"{len(hosts)} given")
    hosts = hosts[:MAX_HOSTS]

    hits, pending = _run_all(_resolves, DNS_NAMES, DNS_TIMEOUT_S)
    res.check(
        "external DNS does not resolve",
        not hits and not pending,
        ", ".join(hits) or f"{pending} lookups did not finish",
    )

    targets: List[Tuple[str, int, int, str]] = []
    for host in [*hosts, *PUBLIC_V4]:
        if host == gate_host:
            continue
        targets += [(host, p, socket.AF_INET, "no_route") for p in PROBE_PORTS]
    # The gate host itself: every port but the orchestrator's must refuse.
    targets += [(gate_host, p, socket.AF_INET, "closed") for p in PROBE_PORTS if p != gate_port]
    # The bridge's usual gateway address (.1): with gateway_mode isolated the
    # host has no address there, so nothing may answer.
    try:
        bridge = str(next(ipaddress.IPv4Network(os.environ.get("JESS_CODER_SUBNET", ""), strict=False).hosts()))
        if bridge != gate_host:
            targets += [(bridge, p, socket.AF_INET, "absent") for p in PROBE_PORTS]
    except (ValueError, StopIteration):
        pass
    # IPv6: public addresses and the bridge's own link-local (host services on [::]).
    v6_hosts = [*PUBLIC_V6, *_parse_link_local(os.environ.get("JESS_SELFTEST_HOSTS_V6", ""))]
    targets += [(h, p, socket.AF_INET6, "no_route") for h in v6_hosts for p in PROBE_PORTS]
    hits, pending = _run_all(_tcp_reach, targets, 60.0)
    res.check(
        f"no TCP reach ({len(targets)} address:port pairs)",
        not hits and not pending,
        ", ".join(hits[:10]) or f"{pending} probes did not finish",
    )

    hits, pending = _run_all(_udp_dns_answers, [*hosts, *PUBLIC_V4], 15.0)
    res.check(
        "no UDP DNS answers", not hits and not pending, ", ".join(hits[:10]) or f"{pending} probes did not finish"
    )


# ----------------------------------------------------------------------- gate


def _gate_from_settings() -> Tuple[str, int]:
    with open(SETTINGS) as fh:
        url = json.load(fh)["modelProviders"]["openai"][0]["baseUrl"]
    parts = urlsplit(url)
    if parts.scheme != "http" or not parts.hostname or not parts.path.rstrip("/").endswith(GATE_PREFIX):
        raise ValueError("baseUrl is not http://<gate>/api/coder/v1")
    ipaddress.IPv4Address(parts.hostname)
    return parts.hostname, parts.port or 80


def _http(host: str, port: int, method: str, path: str, token: Optional[str] = None) -> Optional[int]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = b"{}" if method == "POST" else None
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        resp.read(4096)
        return resp.status
    except OSError:
        return None
    finally:
        conn.close()


def check_gate(res: Results, host: str, port: int) -> None:
    status = _http(host, port, "GET", "/health")
    if not res.check("gate address answers", status is not None, f"no HTTP answer from {host}:{port}"):
        return
    res.check("other paths are refused (403)", status == 403, f"/health -> {status}")
    # Three guard hits per run in all (CoderGuardBlocked's threshold allows for
    # several self-tests in a row): a public path, the main API, a `..` path.
    for method, path in (
        ("POST", "/v1/chat/completions"),
        ("GET", GATE_PREFIX + "/models/../../health"),
    ):
        got = _http(host, port, method, path)
        res.check(f"{method} {path} refused", got in (403, 404), f"-> {got}")
    got = _http(host, port, "GET", GATE_PREFIX + "/models")
    detail = "gate is off: set CODER_ENABLED=true and recreate the orchestrator" if got == 404 else f"-> {got}"
    res.check("gate wants a coder token (401)", got == 401, detail)
    got = _http(host, port, "GET", GATE_PREFIX + "/models", token="jcs_not-a-real-token")
    res.check("gate refuses a bad token (401)", got == 401, f"-> {got}")
    key = os.environ.get("JESS_BRAIN_KEY", "")
    if key:
        got = _http(host, port, "GET", GATE_PREFIX + "/models", token=key)
        res.check("session token works (200)", got == 200, f"-> {got}")


def main() -> int:
    t0 = time.monotonic()
    res = Results()
    try:
        gate_host, gate_port = _gate_from_settings()
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print(f"FAIL settings: {type(exc).__name__}: {exc}")
        print("selftest: FAIL")
        return 1
    for step in (
        check_identity,
        check_filesystem,
        lambda r: check_network_config(r, gate_host),
        lambda r: check_reach(r, gate_host, gate_port),
        lambda r: check_gate(r, gate_host, gate_port),
    ):
        try:
            step(res)
        except Exception as exc:  # noqa: BLE001 - a crashed check is a failed check
            res.check(f"check crashed ({type(exc).__name__})", False, str(exc)[:200])
    took = time.monotonic() - t0
    if res.failed:
        print(f"selftest: FAIL ({len(res.failed)} of {res.count} checks, {took:.1f} s)")
        return 1
    print(f"selftest: PASS ({res.count} checks, {took:.1f} s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    sys.exit(main())
