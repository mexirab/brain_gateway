#!/usr/bin/env python3
"""Find (and with --fix, stop+start) containers whose /mnt bind mounts point at a dead disk.

Why this exists
---------------
On 2026-10-31 the USB enclosure behind /mnt/{logging,documents,media} dropped
for ~3 s and came back under new device names (sdc1 -> sdf1, ...).
usb-storage-remount.service (`mount -a`) remounted every disk on the host within
10 s, so the *MountMissing alerts stayed green. But a bind mount is resolved
once, when a container starts: every container started before the drop kept
the old, shut-down filesystem. Prometheus' WAL writes failed with EIO (every
threshold alert blind), Loki and Grafana could not read their stores, and the
Paperless/Immich Postgres data directories returned I/O errors. It went
unnoticed for three days. Recovery was `docker stop` + `docker start` of each
affected container.

What it does
------------
For every RUNNING container with a bind mount whose host source is under a
watched root (default /mnt), compare
  - the host mount that now holds the source (/proc/1/mountinfo, the host
    mount namespace), with
  - the mount the container actually sees at the destination
    (/proc/<container pid>/mountinfo).
Different device numbers = the container is stale.

  default   check only: log stale containers, write the metrics, exit.
  --fix     also stop+start each stale container, one at a time: monitoring
            first (so a run that is cut short restores alerting first), then
            compose `depends_on` order. Each container is re-checked right before
            its stop, its disk is re-checked between stop and start, and it is
            verified after the start. Repeats (up to 3 passes) when the watched
            host mounts changed during a pass, e.g. a late disk mounted by a
            parallel `mount -a`.

What it will NOT do
-------------------
  - Restart a container that has ANY watched bind whose disk is missing (the
    source resolves to a mount outside the watched roots, i.e. the bare
    mountpoint on the root filesystem). Starting it would write onto the root
    fs under the mountpoint (an empty Postgres data dir = a fresh initdb), the
    2026-07-15 failure. *MountMissing pages for that case. If a disk disappears
    between a stop and its start, the container is left STOPPED, counted as a
    failure, and the run ends.
  - Start a stopped container, or touch a container without a stale bind.
    (`docker compose start` would also try services that were deliberately left
    stopped. On 2026-11-03 that aborted the Immich restart on an ML container
    that cannot start on this host.) Paused, --rm and compose one-off
    containers are reported but not restarted.
  - fsck, mount or unmount anything.

Device numbers are a sound identity here: while any container still holds the
old filesystem the kernel cannot reuse its sdX name, so a re-enumerated disk
always comes back with a different major:minor.

Metrics (--metrics-dir, a node_exporter textfile directory)
-----------------------------------------------------------
jess_stale_mount.prom (every run that could reach dockerd):
  jess_stale_mount_containers                    stale running containers now
  jess_stale_mount_restart_failures              containers a --fix run failed to
                                                 move onto the live disk that are
                                                 STILL not running on it (from
                                                 --state-file; clears by itself
                                                 once they are, however they were
                                                 fixed)
  jess_stale_mount_check_last_run_timestamp_seconds
jess_stale_mount_restart.prom (only --fix runs that restarted at least one):
  jess_stale_mount_restarted_containers
  jess_stale_mount_last_restart_timestamp_seconds
When dockerd is not active nothing is written: the last values stay, and the
frozen timestamp is what StaleMountCheckNotRunning watches.

Exit codes: 0 = nothing stale and no outstanding failures (or dockerd not
active: nothing can be inspected); 1 = stale containers or failures remain;
2 = could not list containers.
Known gap: with live-restore (on since 2026-10-26) containers outlive a dead
dockerd, so a stale one is only seen by the first run after dockerd is back.

Runs as root from usb-storage-restart-stale.service (--fix, started by
usb-storage-remount.service after `mount -a`) and usb-storage-stale-check.timer
(check only). Install steps: header of
scripts/usb-storage/usb-storage-remount.service. Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DOCKER_TIMEOUT_SECONDS = 180
MAX_PASSES = 3
SETTLE_SECONDS = 5
HEALTH_WAIT_SECONDS = 60
# Restored first, so a run that is cut short (timeout, hung dockerd call) has
# already brought alerting back.
PRIORITY_PROJECTS = ("monitoring",)
CHECK_METRICS_FILE = "jess_stale_mount.prom"
RESTART_METRICS_FILE = "jess_stale_mount_restart.prom"


@dataclass(frozen=True)
class Mount:
    dev: str  # "major:minor"
    mountpoint: str
    fstype: str
    source: str


@dataclass(frozen=True)
class StaleBind:
    container_id: str
    name: str
    source: str
    destination: str
    host_mount: Mount
    container_dev: str


@dataclass
class Scan:
    stale: list[StaleBind] = field(default_factory=list)
    # container id -> why it must not be restarted (a watched bind's disk is missing)
    blocked: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def stale_ids(self) -> set[str]:
        return {s.container_id for s in self.stale}


def _unescape(text: str) -> str:
    # mountinfo octal-escapes space, tab, newline and backslash (\040 etc.)
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def parse_mountinfo(text: str) -> list[Mount]:
    """Parse /proc/<pid>/mountinfo, keeping kernel order (later = mounted on top)."""
    mounts: list[Mount] = []
    for line in text.splitlines():
        # Single-space separated; split(" ") keeps an empty source field in place.
        fields = line.split(" ")
        if "-" not in fields:
            continue
        sep = fields.index("-")
        if sep < 6 or len(fields) < sep + 3:
            continue
        mounts.append(
            Mount(
                dev=fields[2],
                mountpoint=_unescape(fields[4]),
                fstype=fields[sep + 1],
                source=_unescape(fields[sep + 2]),
            )
        )
    return mounts


def is_under(path: str, root: str) -> bool:
    path = path.rstrip("/") or "/"
    root = root.rstrip("/") or "/"
    return root == "/" or path == root or path.startswith(root + "/")


def mount_for_path(mounts: list[Mount], path: str) -> Mount | None:
    """The mount that holds `path`: longest mountpoint prefix, topmost on ties."""
    best: Mount | None = None
    for m in mounts:
        if is_under(path, m.mountpoint) and (
            best is None or len(m.mountpoint.rstrip("/")) >= len(best.mountpoint.rstrip("/"))
        ):
            best = m
    return best


def mount_at(mounts: list[Mount], mountpoint: str) -> Mount | None:
    """The topmost mount exactly at `mountpoint`."""
    target = mountpoint.rstrip("/") or "/"
    found = None
    for m in mounts:
        if (m.mountpoint.rstrip("/") or "/") == target:
            found = m
    return found


def _name(container: dict) -> str:
    return (container.get("Name") or container.get("Id", "?")[:12]).lstrip("/")


def watched_binds(container: dict, roots: list[str], realpath=os.path.realpath) -> list[dict]:
    return [
        m
        for m in container.get("Mounts") or []
        if m.get("Type") == "bind" and any(is_under(realpath(m.get("Source", "")), r) for r in roots)
    ]


def disk_present(host_mounts: list[Mount], source: str, roots: list[str]) -> Mount | None:
    """The host mount holding `source` if it is a watched disk, None if the disk is missing."""
    host = mount_for_path(host_mounts, source)
    if host is None or not any(is_under(host.mountpoint, r) for r in roots):
        return None
    return host


def scan(
    containers: list[dict],
    host_mounts: list[Mount],
    read_container_mountinfo,
    roots: list[str],
    realpath=os.path.realpath,
) -> Scan:
    """Classify running containers' watched binds.

    `containers` is `docker inspect` output; `read_container_mountinfo(pid)`
    returns that process's mountinfo text (or raises OSError).
    """
    result = Scan()
    for c in containers:
        state = c.get("State") or {}
        if not state.get("Running") or not state.get("Pid"):
            continue
        name = _name(c)
        binds = watched_binds(c, roots, realpath)
        if not binds:
            continue
        try:
            inner = parse_mountinfo(read_container_mountinfo(state["Pid"]))
        except OSError as exc:
            result.notes.append(f"{name}: cannot read its mountinfo ({exc.__class__.__name__}), skipped")
            continue
        for b in binds:
            source = realpath(b["Source"])
            host = disk_present(host_mounts, source, roots)
            if host is None:
                held = mount_for_path(host_mounts, source)
                reason = (
                    f"{source} is not on a mounted disk (resolves to "
                    f"{held.mountpoint if held else 'nothing'}), not restarting"
                )
                result.blocked[c["Id"]] = reason
                result.notes.append(f"{name}: {reason}")
                continue
            seen = mount_at(inner, b.get("Destination", ""))
            if seen is None:
                result.notes.append(f"{name}: no mount at {b.get('Destination')} inside the container, skipped")
                continue
            if seen.dev != host.dev:
                result.stale.append(
                    StaleBind(
                        container_id=c["Id"],
                        name=name,
                        source=source,
                        destination=b["Destination"],
                        host_mount=host,
                        container_dev=seen.dev,
                    )
                )
    return result


def restart_order(containers: list[dict]) -> list[dict]:
    """Priority projects first, then dependencies before dependents.

    Uses compose's `depends_on` label within a project, format
    "db:service_started:false,broker:service_started:false". Ties and cycles
    fall back to (priority, name) order so the result is deterministic.
    """

    def labels(c: dict) -> dict:
        return (c.get("Config") or {}).get("Labels") or {}

    def key(c: dict) -> tuple[str, str]:
        lb = labels(c)
        return lb.get("com.docker.compose.project", ""), lb.get("com.docker.compose.service", "")

    by_key = {key(c): c for c in containers if key(c)[1]}
    deps: dict[str, set[str]] = {}
    for c in containers:
        project = key(c)[0]
        wanted = set()
        for item in (labels(c).get("com.docker.compose.depends_on") or "").split(","):
            service = item.split(":", 1)[0].strip()
            dep = by_key.get((project, service))
            if service and dep is not None and dep["Id"] != c["Id"]:
                wanted.add(dep["Id"])
        deps[c["Id"]] = wanted

    pending = sorted(
        containers,
        key=lambda c: (key(c)[0] not in PRIORITY_PROJECTS, c.get("Name") or "", c["Id"]),
    )
    ordered: list[dict] = []
    done: set[str] = set()
    while pending:
        ready = [c for c in pending if deps[c["Id"]] <= done] or pending[:1]
        for c in ready:
            ordered.append(c)
            done.add(c["Id"])
        pending = [c for c in pending if c["Id"] not in done]
    return ordered


def _log(msg: str, *, err: bool = False) -> None:
    print(f"[stale-mount] {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=DOCKER_TIMEOUT_SECONDS,
        check=False,
    )


def _read_text(path: str) -> str:
    # surrogateescape: a non-UTF-8 mountpoint must not crash the run
    return Path(path).read_bytes().decode("utf-8", "surrogateescape")


def _read_mountinfo(pid: int) -> str:
    return _read_text(f"/proc/{pid}/mountinfo")


def _host_mounts() -> list[Mount]:
    return parse_mountinfo(_read_text("/proc/1/mountinfo"))


def watched_host_mounts(roots: list[str]) -> tuple[tuple[str, str], ...]:
    return tuple((m.dev, m.mountpoint) for m in _host_mounts() if any(is_under(m.mountpoint, r) for r in roots))


class DockerUnavailable(Exception):
    pass


def _dockerd_inactive() -> bool:
    """True when systemd says docker.service is not running.

    Asked BEFORE the first docker call because docker.socket would otherwise
    socket-activate dockerd at boot, when udev coldplug starts these units.
    """
    if not Path("/run/systemd/system").is_dir():
        return False  # not systemd: let the docker CLI answer
    try:
        state = subprocess.run(
            ["systemctl", "is-active", "docker.service"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return state.stdout.strip() not in ("active", "reloading")


def running_containers() -> list[dict]:
    if _dockerd_inactive():
        raise DockerUnavailable("docker.service is not active")
    try:
        ps = _docker("ps", "-q", "--no-trunc")
    except FileNotFoundError as exc:
        raise DockerUnavailable("docker CLI not found") from exc
    if ps.returncode != 0:
        if "Cannot connect to the Docker daemon" in ps.stderr:
            raise DockerUnavailable("docker daemon not running")
        raise RuntimeError(f"docker ps failed: {ps.stderr.strip()[:200]}")
    ids = ps.stdout.split()
    if not ids:
        return []
    out = _docker("inspect", *ids)
    if out.returncode == 0:
        return json.loads(out.stdout)
    # A container can exit between ps and inspect; inspect them one by one.
    return [c for c in (inspect_one(cid) for cid in ids) if c is not None]


def inspect_one(cid: str) -> dict | None:
    """The container's inspect data, or None if it no longer exists."""
    out = _docker("inspect", cid)
    if out.returncode != 0:
        return None
    data = json.loads(out.stdout)
    return data[0] if data else None


def _wait_until_settled(cid: str) -> dict | None:
    """Give a just-started container time to crash or report health."""
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    time.sleep(SETTLE_SECONDS)
    c = inspect_one(cid)
    while c is not None and time.monotonic() < deadline:
        health = ((c.get("State") or {}).get("Health") or {}).get("Status")
        if health in (None, "healthy", "unhealthy"):
            break
        time.sleep(2)
        c = inspect_one(cid)
    return c


def restart_one(cid: str, roots: list[str]) -> str:
    """stop + start one container. Returns "ok", "skipped", "failed" or "abort".

    "abort" = a disk vanished between stop and start: the container is left
    stopped and the caller must not restart anything else this run.
    """
    c = inspect_one(cid)
    if c is None or not (c.get("State") or {}).get("Running"):
        _log(f"{cid[:12]}: gone or no longer running, skipped")
        return "skipped"
    name, state = _name(c), c.get("State") or {}
    labels = (c.get("Config") or {}).get("Labels") or {}
    if (
        state.get("Paused")
        or (c.get("HostConfig") or {}).get("AutoRemove")
        or labels.get("com.docker.compose.oneoff") == "True"
    ):
        _log(f"{name}: paused, --rm or one-off container, not restarting (still stale)")
        return "skipped"
    # Re-check right before acting: earlier restarts take minutes and the
    # enclosure can drop again, or the container may have been recreated.
    now = scan([c], _host_mounts(), _read_mountinfo, roots)
    if cid in now.blocked:
        _log(f"{name}: {now.blocked[cid]}")
        return "skipped"
    if cid not in now.stale_ids:
        _log(f"{name}: no longer stale, skipped")
        return "skipped"
    expected = {s.source: s.host_mount.dev for s in now.stale}

    stop = _docker("stop", cid)
    if stop.returncode != 0:
        _log(f"{name}: docker stop failed: {stop.stderr.strip()[:200]}", err=True)
    host = _host_mounts()
    for b in watched_binds(c, roots):
        source = os.path.realpath(b["Source"])
        m = disk_present(host, source, roots)
        if m is None or (source in expected and m.dev != expected[source]):
            _log(f"{name}: {source} changed or vanished while stopping; leaving it STOPPED", err=True)
            return "abort"
    start = _docker("start", cid)
    if start.returncode != 0:
        _log(f"{name}: docker start FAILED: {start.stderr.strip()[:200]}", err=True)
        return "failed"

    after = _wait_until_settled(cid)
    after_state = (after or {}).get("State") or {}
    if after is None or not after_state.get("Running") or after_state.get("Restarting"):
        _log(f"{name}: not running after start", err=True)
        return "failed"
    # A manual `docker start` resets RestartCount to 0 (checked on Docker 29.1.3), so
    # comparing with the count before the stop would miss a crash of a container
    # that had restarted before: any count now is a crash after our start.
    if after.get("RestartCount", 0) > 0:
        _log(f"{name}: restarted by its restart policy after our start (crashing), or never stopped", err=True)
        return "failed"
    if (after_state.get("Health") or {}).get("Status") == "unhealthy":
        _log(f"{name}: unhealthy after start", err=True)
        return "failed"
    check = scan([after], _host_mounts(), _read_mountinfo, roots)
    if cid in check.stale_ids or cid in check.blocked:
        _log(f"{name}: STILL not on the live disk after restart", err=True)
        return "failed"
    _log(f"{name}: restarted, now on the live disk")
    return "ok"


@dataclass
class Run:
    roots: list[str]
    deadline: float
    failed: dict[str, str]  # container id -> name, from --state-file
    restarted_ok: int = 0
    last_scan: Scan = field(default_factory=Scan)
    # restarted OK since last_scan was taken (a cut-short run reports these as fixed)
    fixed_since_scan: set[str] = field(default_factory=set)
    running_ids: set[str] = field(default_factory=set)


def log_scan(s: Scan) -> None:
    for note in s.notes:
        _log(note)
    for b in s.stale:
        _log(
            f"STALE {b.name}: {b.destination} is on device {b.container_dev}, "
            f"host {b.host_mount.mountpoint} is now {b.host_mount.source} ({b.host_mount.dev})"
        )


def fix(run: Run) -> None:
    for pass_no in range(1, MAX_PASSES + 1):
        before = watched_host_mounts(run.roots)
        containers = running_containers()
        s = scan(containers, _host_mounts(), _read_mountinfo, run.roots)
        run.last_scan, run.running_ids = s, {c["Id"] for c in containers}
        run.fixed_since_scan.clear()
        if pass_no > 1:
            log_scan(s)
        targets = [c for c in containers if c["Id"] in s.stale_ids and c["Id"] not in s.blocked]
        if not targets:
            return
        for c in restart_order(targets):
            if time.monotonic() > run.deadline:
                _log("run deadline reached; remaining containers stay stale", err=True)
                return
            # Recorded as failed BEFORE it is touched: a SIGTERM (TimeoutStartSec)
            # between its stop and its start must not leave a stopped container
            # that no metric mentions. A false entry from a run killed after a
            # good start clears at the next 10-min check (running + on the live disk).
            was_failed = c["Id"] in run.failed
            run.failed[c["Id"]] = _name(c)
            try:
                result = restart_one(c["Id"], run.roots)
            except Exception as exc:  # a hung/failed docker call must not end the queue
                _log(f"{_name(c)}: restart raised {exc.__class__.__name__}: {exc}", err=True)
                result = "failed"
            if result == "ok":
                run.restarted_ok += 1
                run.fixed_since_scan.add(c["Id"])
                run.failed.pop(c["Id"], None)
            elif result == "skipped" and not was_failed:
                run.failed.pop(c["Id"], None)
            if result == "abort":
                return
        if watched_host_mounts(run.roots) == before:
            return
        _log("watched host mounts changed during the pass; checking again")


def unresolved_failures(run: Run) -> dict[str, str]:
    """Recorded failures whose container is still not running on the live disk."""
    out = {}
    for cid, name in run.failed.items():
        if cid in run.running_ids:
            if cid in run.fixed_since_scan or (cid not in run.last_scan.stale_ids and cid not in run.last_scan.blocked):
                continue  # running on the live disk again, however it got there
        else:
            try:
                if inspect_one(cid) is None:
                    continue  # removed (or recreated under a new id)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        out[cid] = name
    return out


def _write_atomic(path: Path, text: str, mode: int) -> None:
    """Write via a random O_EXCL temp file + rename: never follows a planted symlink
    (root writes into a directory labadmin owns), never collides with a parallel run."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_failed(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return {}
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = json.loads(f.read(1_000_000) or b"{}")
        failed = data.get("failed") if isinstance(data, dict) else None
        return {str(k): str(v) for k, v in failed.items()} if isinstance(failed, dict) else {}
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)


def check_metrics(now: float, stale_count: int, failures: int) -> str:
    return (
        "\n".join(
            [
                "# HELP jess_stale_mount_containers Running containers whose /mnt bind mount points at a different device than the host mount (dead disk).",
                "# TYPE jess_stale_mount_containers gauge",
                f"jess_stale_mount_containers {stale_count}",
                "# HELP jess_stale_mount_restart_failures Containers an automatic restart failed to move onto the live disk that still are not running on it.",
                "# TYPE jess_stale_mount_restart_failures gauge",
                f"jess_stale_mount_restart_failures {failures}",
                "# HELP jess_stale_mount_check_last_run_timestamp_seconds Last run of restart_stale_mount_containers.py that could reach dockerd.",
                "# TYPE jess_stale_mount_check_last_run_timestamp_seconds gauge",
                f"jess_stale_mount_check_last_run_timestamp_seconds {now:.0f}",
            ]
        )
        + "\n"
    )


def restart_metrics(now: float, restarted: int) -> str:
    return (
        "\n".join(
            [
                "# HELP jess_stale_mount_restarted_containers Containers the last restarting --fix run moved onto the live disk.",
                "# TYPE jess_stale_mount_restarted_containers gauge",
                f"jess_stale_mount_restarted_containers {restarted}",
                "# HELP jess_stale_mount_last_restart_timestamp_seconds Last --fix run that moved at least one container onto the live disk.",
                "# TYPE jess_stale_mount_last_restart_timestamp_seconds gauge",
                f"jess_stale_mount_last_restart_timestamp_seconds {now:.0f}",
            ]
        )
        + "\n"
    )


def _lock(directory: Path, blocking: bool) -> int | None:
    """flock the metrics directory so a check never overlaps a fix run.

    Returns the fd to keep, or None when non-blocking and already held.
    """
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _terminate(signum, frame):  # noqa: ARG001
    # systemd's TimeoutStartSec sends SIGTERM: unwind so the finally block
    # still writes metrics and the failure state.
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--fix", action="store_true", help="stop+start stale containers")
    ap.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="watch bind sources under this directory (repeatable; default /mnt)",
    )
    ap.add_argument("--metrics-dir", type=Path, help="node_exporter textfile collector directory")
    ap.add_argument("--state-file", type=Path, help="JSON of containers a --fix run failed to restart")
    ap.add_argument("--deadline-seconds", type=int, default=900, help="--fix stops starting new restarts after this")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)

    lock_fd = None
    if args.metrics_dir:
        lock_fd = _lock(args.metrics_dir, blocking=args.fix)
        if lock_fd is None:
            _log("a --fix run is in progress; skipping this check")
            return 0

    run = Run(
        roots=[os.path.realpath(r) for r in (args.roots or ["/mnt"])],
        deadline=time.monotonic() + args.deadline_seconds,
        failed=load_failed(args.state_file),
    )
    now = time.time()
    try:
        containers = running_containers()
    except DockerUnavailable as exc:
        _log(f"{exc}: nothing can be inspected, metrics left as they were")
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _log(f"cannot list containers: {exc}", err=True)
        return 2

    run.last_scan = scan(containers, _host_mounts(), _read_mountinfo, run.roots)
    run.running_ids = {c["Id"] for c in containers}
    log_scan(run.last_scan)
    failures: dict[str, str] = {}
    try:
        if args.fix and run.last_scan.stale_ids:
            fix(run)
            final = running_containers()
            run.last_scan = scan(final, _host_mounts(), _read_mountinfo, run.roots)
            run.running_ids = {c["Id"] for c in final}
            run.fixed_since_scan.clear()
    except (DockerUnavailable, RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        _log(f"re-check failed ({exc.__class__.__name__}); reporting the last known state", err=True)
    finally:
        failures = unresolved_failures(run)
        stale_count = len(run.last_scan.stale_ids - run.fixed_since_scan)
        if args.metrics_dir:
            _write_atomic(args.metrics_dir / CHECK_METRICS_FILE, check_metrics(now, stale_count, len(failures)), 0o644)
            if run.restarted_ok:
                _write_atomic(args.metrics_dir / RESTART_METRICS_FILE, restart_metrics(now, run.restarted_ok), 0o644)
        if args.fix and args.state_file:
            _write_atomic(args.state_file, json.dumps({"failed": failures}, indent=1) + "\n", 0o600)
        for cid, name in failures.items():
            _log(f"FAILED earlier and still not on the live disk: {name} ({cid[:12]})", err=True)
        if lock_fd is not None:
            os.close(lock_fd)

    _log(
        f"done: {stale_count} stale, {len(failures)} unresolved failure(s)"
        + (f", {run.restarted_ok} restarted" if args.fix else "")
    )
    return 1 if stale_count or failures else 0


if __name__ == "__main__":
    sys.exit(main())
