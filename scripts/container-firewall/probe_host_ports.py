"""Which Jupiter host ports can a container on this Docker network reach? (read-only probe)

Runs INSIDE a throwaway container, one per network, e.g. from the repo root:

    for n in $(docker network ls --filter driver=bridge --format '{{.Name}}'); do
      docker run --rm --cap-drop ALL --security-opt no-new-privileges --network "$n" \
        -v "$PWD/scripts/container-firewall/probe_host_ports.py:/probe.py:ro" \
        --entrypoint python gateway_nerves-orchestrator -I /probe.py --label "$n"
    done

Prints one JSON line per network: for each host address (the network's own
gateway, the LAN IP, docker0, plus any --extra name=ip, e.g. the tailnet IP) the TCP ports that accepted a
connection, plus whether a DNS query to port 53 got an answer. It only opens and
closes TCP connections and sends one DNS query per address. No payload, no
logins. Background: docs/internal/CONTAINER_HOST_REACHABILITY.md.

With jess-docker-input in observe mode every port still answers (only logged);
in enforce mode only the allow-listed (network, port) pairs should. Note that a
one-network probe container leaves through that network; a container on several
networks leaves through its default-route network (compose gw_priority), so check
those (orchestrator, Prometheus, blackbox) from the real containers too. IPv4 only.
"""

from __future__ import annotations

import argparse
import errno
import json
import random
import socket
import struct
from concurrent.futures import ThreadPoolExecutor

PORTS = [22, 53, 139, 443, 445, 2283, 3000, 3001, 5006, 5007, 5050, 5232, 7575, 8004, 8053, 8090,
         8123, 8125, 8484, 8888, 9100, 9115, 9400, 13378, 18555, 19999]  # fmt: skip
HOST_ADDRESSES = {"lan": "10.0.0.248", "docker0": "172.17.0.1"}  # more with --extra name=ip


def default_gateway() -> str:
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":
                return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    # internal network: no default route; Docker gives the bridge .1 of the subnet
    ip = socket.gethostbyname(socket.gethostname())
    return ".".join(ip.split(".")[:3]) + ".1"


def tcp(host: str, port: int) -> str:
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((host, port))
        return "open"
    except TimeoutError:
        return "timeout"
    except ConnectionRefusedError:
        return "refused"
    except OSError as e:
        return {errno.ENETUNREACH: "netunreach", errno.EHOSTUNREACH: "hostunreach"}.get(e.errno, f"err{e.errno}")
    finally:
        s.close()


def dns(host: str) -> str:
    query = struct.pack(">HHHHHH", random.randint(0, 65535), 0x0100, 1, 0, 0, 0)
    query += b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.sendto(query, (host, 53))
        reply = s.recv(512)
        return f"answer(rcode={reply[3] & 0xF},an={struct.unpack('>H', reply[6:8])[0]})"
    except TimeoutError:
        return "timeout"
    except OSError as e:
        return f"err{e.errno}"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="", help="network name to print with the result")
    ap.add_argument("--extra", action="append", default=[], help="another host address to try, as name=ip")
    args = ap.parse_args()
    targets = {"gateway": default_gateway(), **HOST_ADDRESSES}
    for item in args.extra:
        name, _, ip = item.partition("=")
        targets[name] = socket.inet_ntoa(socket.inet_aton(ip))  # refuses anything but an IPv4 address
    out: dict = {"network": args.label, "gateway_ip": targets["gateway"]}
    with ThreadPoolExecutor(64) as ex:
        for name, host in targets.items():
            states = dict(zip(PORTS, ex.map(lambda p, h=host: tcp(h, p), PORTS), strict=True))
            out[name] = {
                "open": [p for p, st in states.items() if st == "open"],
                "other": {p: st for p, st in states.items() if st not in ("open", "refused")},
                "dns53": dns(host),
            }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
