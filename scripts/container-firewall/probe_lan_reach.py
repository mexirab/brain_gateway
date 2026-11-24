"""What can a container on this Docker network reach OFF Jupiter? (read-only probe)

The FORWARD-path companion of probe_host_ports.py: LAN hosts, the router, the
tailnet, Jupiter's own published ports via its LAN IP, and the internet. Runs
INSIDE a throwaway container, one per network, e.g. from the repo root:

    for n in $(docker network ls --filter driver=bridge --format '{{.Name}}'); do
      docker run --rm --cap-drop ALL --security-opt no-new-privileges --network "$n" \
        -v "$PWD/scripts/container-firewall/probe_lan_reach.py:/probe.py:ro" \
        --entrypoint python gateway_nerves-orchestrator -I /probe.py --label "$n"
    done

Prints one JSON line per network. Per target, TCP ports by result:
- `open`: the connection was accepted.
- `refused`: a TCP reset came back. Usually the path works and only the port is
  closed. Exception: from the network that publishes a Jupiter port, the reset
  can be JESS-DOCKER-IN's (that traffic goes through INPUT, not FORWARD).
- `blocked`: an ICMP error came back (the jess-docker-input FORWARD chain in
  enforce mode, or a router answering for a dead host), or the container has no
  route at all (internal networks fail at once).
- `timeout`: no answer.
In enforce mode read `blocked` and `timeout` together as "not through": the
kernel sends only a few ICMP errors per second to one container, so most
rejects in a burst look like timeouts. --recheck retries the timeouts one at a
time, 1.1 s apart, which turns rejects back into `blocked`.

Also one DNS query (`example.com A`) to targets that serve DNS, the container's
own resolver lookup, and one NAT-PMP "external address" request (opcode 0, the
read-only request) to the router. The reply's address is never read or printed.
Connected UDP only reports "hard" ICMP errors (e.g. admin-prohibited); a plain
host-unreachable shows as `timeout`.

It only opens and closes TCP connections and sends single UDP queries. No
payload, no logins, no port mappings. Extra targets with --target name=ip (the
default port list) or name=ip:p1,p2. Use it for the voice satellites
(VOICE_SATELLITE_DEVICES, port 6053), tailnet peers and the router's WAN
address, which stay out of this public file. Labels are printed, addresses of
--target entries are not (--show-addresses to print them).

A one-network probe container leaves through that network. A container on
several networks leaves through its default-route network (compose
gw_priority), so check the real orchestrator/Prometheus/blackbox containers
too. Runs with --cap-drop ALL, so it can't test raw-socket tricks. IPv4 only
(IPv6 forwarding is off on Jupiter). Background:
docs/internal/CONTAINER_LAN_REACHABILITY.md.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import random
import socket
import struct
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORTS = [22, 53, 80, 443, 8080, 8443]
RESERVED = {"network", "resolver"}  # top-level keys of the output line
NO_ROUTE = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM}
# name: (address, tcp ports, udp checks). Addresses already in the repo's docs.
TARGETS: dict[str, tuple[str, list[int], list[str]]] = {
    "router": ("10.0.0.1", [22, 23, 53, 80, 443, 5000, 8080, 8443], ["dns", "natpmp"]),
    "helios": ("10.0.0.195", [22, 8003, 8004, 8080, 9080, 9100, 9199, 9400, 10300, 10301, 19999], []),
    "saturn": ("10.0.0.58", [22, 53, 8053, 9100, 9400, 9466, 19999], ["dns"]),
    "uranus": ("10.0.0.173", [22, 9100, 19999], []),
    "jupiter-lan-published": ("10.0.0.248", [2283, 3101, 8777, 8888, 13378], []),
    "pcp-anycast": ("192.0.0.9", [], ["natpmp"]),
    "internet-1.1.1.1": ("1.1.1.1", [53, 80, 443], ["dns"]),
    "internet-9.9.9.9": ("9.9.9.9", [443], []),
}


def tcp(host: str, port: int) -> str:
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect((host, port))
        return "open"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return "blocked" if e.errno in NO_ROUTE else f"err{e.errno}"
    finally:
        s.close()


def _udp(host: str, port: int, payload: bytes) -> bytes | str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    try:
        s.connect((host, port))  # connected UDP: a hard ICMP error surfaces as an exception
        s.send(payload)
        return s.recv(512)
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return "blocked" if e.errno in NO_ROUTE else f"err{e.errno}"
    finally:
        s.close()


def dns(host: str) -> str:
    qid = random.randint(0, 65535)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    query += b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    reply = _udp(host, 53, query)
    if isinstance(reply, str):
        return reply
    if len(reply) < 12 or struct.unpack(">H", reply[:2])[0] != qid:
        return "answer(malformed)"
    return f"answer(rcode={reply[3] & 0xF},an={struct.unpack('>H', reply[6:8])[0]})"


def natpmp(host: str) -> str:
    """NAT-PMP opcode 0 = 'what is your external address' (RFC 6886 3.2). Read-only.

    Only bytes 1-3 (opcode, result code) are read; bytes 8-11 (the address) never are.
    """
    reply = _udp(host, 5351, b"\x00\x00")
    if isinstance(reply, str):
        return reply
    if len(reply) >= 4 and reply[1] == 128:
        return f"answer(result={struct.unpack('>H', reply[2:4])[0]})"  # 0 = NAT-PMP is on
    return "answer(unexpected)"


UDP_CHECKS = {"dns": dns, "natpmp": natpmp}


def resolver() -> str:
    try:
        return f"ok({len(socket.getaddrinfo('example.com', 443, socket.AF_INET, socket.SOCK_STREAM))})"
    except socket.gaierror as e:
        return f"fail({e.errno})"


def parse_target(ap: argparse.ArgumentParser, item: str) -> tuple[str, str, list[int]]:
    name, sep, rest = item.partition("=")
    if not sep or not name or name in RESERVED:
        ap.error(f"--target needs name=ip[:port,...] with a name other than {sorted(RESERVED)}")
    ip, _, ports_s = rest.partition(":")
    try:
        ip = str(ipaddress.IPv4Address(ip))  # dotted-quad IPv4 only
    except ValueError:
        ap.error(f"--target {name}: not an IPv4 address")
    ports = DEFAULT_PORTS
    if ports_s:
        try:
            ports = [int(p) for p in ports_s.split(",")]
        except ValueError:
            ap.error(f"--target {name}: ports must be numbers separated by commas")
        if any(not 1 <= p <= 65535 for p in ports):
            ap.error(f"--target {name}: ports must be 1-65535")
    return name, ip, ports


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="", help="network name to print with the result")
    ap.add_argument("--target", action="append", default=[], help="name=ip or name=ip:port,port")
    ap.add_argument("--only", default="", help="comma-separated target names to probe (default: all)")
    ap.add_argument("--recheck", action="store_true", help="retry timed-out ports one at a time, 1.1 s apart")
    ap.add_argument("--show-addresses", action="store_true", help="also print the --target addresses")
    args = ap.parse_args()
    targets = dict(TARGETS)
    hidden: set[str] = set()
    for item in args.target:
        name, ip, ports = parse_target(ap, item)
        targets[name] = (ip, ports, [])
        if not args.show_addresses:
            hidden.add(name)
    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        if unknown := wanted - targets.keys():
            ap.error(f"--only: unknown target(s) {sorted(unknown)}")
        targets = {k: v for k, v in targets.items() if k in wanted}
    out: dict = {"network": args.label, "resolver": resolver()}
    with ThreadPoolExecutor(64) as ex:
        jobs = {
            name: ({p: ex.submit(tcp, ip, p) for p in ports}, {u: ex.submit(UDP_CHECKS[u], ip) for u in udps})
            for name, (ip, ports, udps) in targets.items()
        }
        results = {name: ({p: f.result() for p, f in tj.items()}, {u: f.result() for u, f in uj.items()})
                   for name, (tj, uj) in jobs.items()}  # fmt: skip
    if args.recheck:
        for name, (states, _) in results.items():
            for p, st in states.items():
                if st == "timeout":
                    time.sleep(1.1)  # past the kernel's ICMP rate limit for this source
                    states[p] = tcp(targets[name][0], p)
    for name, (states, udp_results) in results.items():
        entry: dict = {} if name in hidden else {"ip": targets[name][0]}
        for kind in ("open", "refused", "blocked", "timeout"):
            hits = [p for p, st in states.items() if st == kind]
            if hits:
                entry[kind] = hits
        other = {p: st for p, st in states.items() if st not in ("open", "refused", "blocked", "timeout")}
        if other:
            entry["other"] = other
        entry.update(udp_results)
        out[name] = entry
    print(json.dumps(out))


if __name__ == "__main__":
    main()
