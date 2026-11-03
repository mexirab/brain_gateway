"""
Tests for scripts/restart_stale_mount_containers.py — the host-side safety net
that stop+starts containers still bind-mounting a disconnected /mnt disk
(2026-10-31: Prometheus/Loki/Grafana/Paperless/Immich ran on a dead filesystem
for three days although the host had already remounted the disks).

No root, docker or real /proc: a small in-memory World fakes the docker CLI
(ps / inspect / stop / start), the host mount table (/proc/1/mountinfo) and each
container's mountinfo. A container's binds resolve against the host mounts at
the moment it starts, exactly like the real thing, so "re-plug a disk under a
new device number" makes every running container on it stale. Any docker
command other than ps/inspect/stop/start is recorded as unexpected and fails
the test, so "never runs anything else" is enforced everywhere.

The module's `time` is replaced by a fake clock (sleeps advance it instantly)
and its `signal` by a recorder (main() must not install a SIGTERM handler in
the pytest process).

Paths live under /mnt/jt-*, which exist neither on the host nor in the test
image, so os.path.realpath() leaves them unchanged.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import itertools
import json
import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "restart_stale_mount_containers.py"


def _load():
    spec = importlib.util.spec_from_file_location("restart_stale_mount_containers_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Must be registered BEFORE exec: @dataclass resolves the string annotations
    # (from __future__ import annotations) via sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rsm = _load()
Mount = rsm.Mount

ROOT = "/mnt"
LOG = "/mnt/jt-logging"
DOCS = "/mnt/jt-documents"
ROOT_FS_DEV = "259:2"
OLD_LOG_DEV, NEW_LOG_DEV = "8:33", "8:81"
DOCS_DEV = "8:49"
WALL = 1_800_000_000.0

CHECK_PROM = "jess_stale_mount.prom"
RESTART_PROM = "jess_stale_mount_restart.prom"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _cp(args, rc=0, out="", err=""):
    return subprocess.CompletedProcess(["docker", *args], rc, out, err)


class FakeClock:
    """Stands in for the module's `time`: sleep() advances monotonic time instantly."""

    def __init__(self):
        self.mono = 10_000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.mono

    def time(self):
        return WALL

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.mono += seconds


class World:
    def __init__(self):
        self.disks: dict[str, tuple[str, str]] = {}  # host mountpoint -> (dev, source)
        self.containers: dict[str, dict] = {}  # id -> docker inspect dict
        self.inner: dict[int, dict[str, str]] = {}  # pid -> destination -> dev it sees
        self.unreadable: set[int] = set()
        self.calls: list[tuple[str, ...]] = []
        self.unexpected: list[tuple[str, ...]] = []
        self.fail: dict[tuple[str, str], object] = {}  # (cmd, id) -> stderr or exception
        self.hooks: dict[tuple[str, str], object] = {}  # (cmd, id) -> fn(container dict)
        self.health: dict[str, list[str]] = {}  # id -> health statuses served by inspect
        self.clock = FakeClock()
        self.signals: list[tuple] = []
        self._pid = 1000

    # -- host side --------------------------------------------------------
    def plug(self, mountpoint, dev, source=None):
        self.disks[mountpoint] = (dev, source or "/dev/disk-" + dev.replace(":", "-"))

    def unplug(self, mountpoint):
        del self.disks[mountpoint]

    def host_text(self):
        lines = [
            f"1 0 {ROOT_FS_DEV} / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw,errors=remount-ro",
            "22 1 0:21 / /proc rw,nosuid,nodev,noexec,relatime shared:12 - proc proc rw",
        ]
        for i, (mp, (dev, src)) in enumerate(self.disks.items()):
            lines.append(f"{200 + i} 1 {dev} / {mp} rw,relatime shared:{200 + i} - ext4 {src} rw")
        return "\n".join(lines) + "\n"

    def host_mounts(self):
        return rsm.parse_mountinfo(self.host_text())

    def dev_now(self, source):
        return rsm.mount_for_path(self.host_mounts(), source).dev

    # -- containers -------------------------------------------------------
    def add(self, name, binds, *, project="", service="", depends_on="", labels=None, running=True):
        cid = hashlib.sha256(name.encode()).hexdigest()
        lb = dict(labels or {})
        if project:
            lb["com.docker.compose.project"] = project
        if service:
            lb["com.docker.compose.service"] = service
        if depends_on:
            lb["com.docker.compose.depends_on"] = depends_on
        self.containers[cid] = {
            "Id": cid,
            "Name": "/" + name,
            "RestartCount": 0,
            "State": {"Status": "exited", "Running": False, "Paused": False, "Restarting": False, "Pid": 0},
            "HostConfig": {"AutoRemove": False},
            "Config": {"Labels": lb},
            "Mounts": [{"Type": "bind", "Source": s, "Destination": d, "RW": True} for s, d in binds],
        }
        if running:
            self.boot(cid)
        return cid

    def boot(self, cid):
        """Start the container's process: its binds resolve against the host mounts NOW."""
        c = self.containers[cid]
        self._pid += 1
        c["State"].update(Status="running", Running=True, Pid=self._pid)
        self.inner[self._pid] = {m["Destination"]: self.dev_now(m["Source"]) for m in c["Mounts"]}

    def halt(self, cid):
        c = self.containers[cid]
        self.inner.pop(c["State"]["Pid"], None)
        c["State"].update(Status="exited", Running=False, Pid=0)

    def pid(self, cid):
        return self.containers[cid]["State"]["Pid"]

    def inspect_all(self):
        return [copy.deepcopy(c) for c in self.containers.values()]

    def read_mountinfo(self, pid):
        if pid in self.unreadable:
            raise PermissionError(13, "Permission denied")
        if pid not in self.inner:
            raise FileNotFoundError(2, "No such file or directory")
        lines = [f"700 650 0:{60 + pid % 100} / / rw,relatime - overlay overlay rw,lowerdir=/x"]
        for i, (dst, dev) in enumerate(self.inner[pid].items()):
            lines.append(f"{710 + i} 700 {dev} /data{i} {dst} rw,relatime - ext4 /dev/sdx1 rw")
        return "\n".join(lines) + "\n"

    # -- docker CLI -------------------------------------------------------
    def docker(self, *args):
        self.calls.append(args)
        cmd, rest = args[0], args[1:]
        if cmd == "ps" and rest == ("-q", "--no-trunc"):
            failure = self.fail.get(("ps", ""))
            if isinstance(failure, BaseException):
                raise failure
            if failure is not None:
                return _cp(args, 1, "", failure)
            out = "".join(f"{cid}\n" for cid, c in self.containers.items() if c["State"]["Running"])
            hook = self.hooks.get(("ps", ""))
            if hook:
                hook(None)
            return _cp(args, 0, out)
        if cmd == "inspect" and rest:
            found = []
            for cid in rest:
                c = self.containers.get(cid)
                if c is None:
                    continue
                if self.health.get(cid):
                    c["State"]["Health"] = {"Status": self.health[cid].pop(0)}
                found.append(copy.deepcopy(c))
            if len(found) != len(rest):
                return _cp(args, 1, json.dumps(found), "Error: No such object")
            return _cp(args, 0, json.dumps(found))
        if cmd in ("stop", "start") and len(rest) == 1:
            cid = rest[0]
            failure = self.fail.get((cmd, cid))
            if isinstance(failure, BaseException):
                raise failure
            if failure is not None:
                return _cp(args, 1, "", failure)
            if cid not in self.containers:
                return _cp(args, 1, "", "Error response from daemon: No such container")
            running = self.containers[cid]["State"]["Running"]
            if cmd == "stop" and running:
                self.halt(cid)
            if cmd == "start" and not running:
                # Like dockerd: a manual start resets RestartCount (verified on Docker 29.1.3).
                self.containers[cid]["RestartCount"] = 0
                self.boot(cid)
            hook = self.hooks.get((cmd, cid))
            if hook:
                hook(self.containers.get(cid))
            return _cp(args, 0, cid + "\n")
        self.unexpected.append(args)
        return _cp(args, 1, "", "unexpected docker command in test")

    @property
    def actions(self):
        return [(a[0], a[1]) for a in self.calls if a[0] in ("stop", "start")]


@pytest.fixture
def world(monkeypatch):
    for p in (LOG, DOCS):
        assert os.path.realpath(p) == p, f"{p} exists here; these tests need it to be a plain path"
    w = World()
    w.plug(LOG, OLD_LOG_DEV, "/dev/sdc1")
    w.plug(DOCS, DOCS_DEV, "/dev/sdd1")
    monkeypatch.setattr(rsm, "_docker", w.docker)
    monkeypatch.setattr(rsm, "_host_mounts", w.host_mounts)
    monkeypatch.setattr(rsm, "_read_mountinfo", w.read_mountinfo)
    monkeypatch.setattr(rsm, "_dockerd_inactive", lambda: False)
    monkeypatch.setattr(rsm, "time", w.clock)
    monkeypatch.setattr(
        rsm,
        "signal",
        SimpleNamespace(SIGTERM=signal.SIGTERM, signal=lambda signum, handler: w.signals.append((signum, handler))),
    )
    yield w
    assert not w.unexpected, f"script ran unexpected docker commands: {w.unexpected}"


def replug_log(w):
    """The USB enclosure dropped and came back: /mnt/jt-logging is now a new device."""
    w.plug(LOG, NEW_LOG_DEV, "/dev/sdf1")


@pytest.fixture
def stale(world):
    """A running compose container with a stale /data (LOG) and a healthy /docs (DOCS) bind."""
    cid = world.add(
        "jt-app",
        [(LOG + "/app", "/data"), (DOCS + "/app", "/docs")],
        project="jtproj",
        service="app",
        labels={"com.docker.compose.oneoff": "False"},
    )
    replug_log(world)
    return cid


def read_prom(path):
    values, helps, types = {}, set(), {}
    for line in path.read_text().splitlines():
        if line.startswith("# HELP "):
            helps.add(line.split()[2])
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            types[name] = kind
        elif line:
            name, value = line.split()
            values[name] = float(value)
    assert set(values) == helps == set(types), "every sample needs HELP and TYPE"
    assert set(types.values()) == {"gauge"}
    return values


def tmp_leftovers(*dirs):
    return [p for d in dirs for p in d.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]


def main(*args):
    return rsm.main([str(a) for a in args])


@pytest.fixture
def mdir(tmp_path):
    d = tmp_path / "textfile_collector"
    d.mkdir()
    return d


@pytest.fixture
def state_file(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d / "failed.json"


# --------------------------------------------------------------------------
# 1. parse_mountinfo
# --------------------------------------------------------------------------


def test_parse_mountinfo_handles_any_number_of_optional_fields():
    text = (
        "36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 - ext3 /dev/root rw,errors=continue\n"
        "37 35 8:33 / /mnt/jt-logging rw,relatime shared:5 master:7 propagate_from:2 unbindable - ext4 /dev/sdc1 rw\n"
        "38 35 8:49 / /mnt/jt-documents rw,relatime - xfs /dev/sdd1 rw,attr2\n"
    )
    assert rsm.parse_mountinfo(text) == [
        Mount("98:0", "/mnt/parent", "ext3", "/dev/root"),
        Mount("8:33", "/mnt/jt-logging", "ext4", "/dev/sdc1"),
        Mount("8:49", "/mnt/jt-documents", "xfs", "/dev/sdd1"),
    ]


@pytest.mark.parametrize(
    "raw, expected",
    [
        (r"/mnt/my\040disk", "/mnt/my disk"),
        (r"/mnt/back\134slash", "/mnt/back\\slash"),
        # an escaped backslash followed by the digits 040 is NOT a space
        (r"/mnt/x\134040", "/mnt/x\\040"),
        (r"/mnt/tab\011nl\012end", "/mnt/tab\tnl\nend"),
    ],
)
def test_parse_mountinfo_unescapes_octal_sequences(raw, expected):
    line = f"40 1 8:1 / {raw} rw,relatime shared:9 - ext4 //nas/share\\040x rw\n"
    (m,) = rsm.parse_mountinfo(line)
    assert m.mountpoint == expected
    assert m.source == "//nas/share x"


def test_parse_mountinfo_empty_source_does_not_shift_fields():
    text = (
        "45 30 0:41 / /run/user/1000 rw,nosuid shared:300 - tmpfs  rw,size=1k\n"
        "46 30 8:33 / /mnt/jt-logging rw - ext4 /dev/sdc1 rw\n"
    )
    assert rsm.parse_mountinfo(text) == [
        Mount("0:41", "/run/user/1000", "tmpfs", ""),
        Mount("8:33", "/mnt/jt-logging", "ext4", "/dev/sdc1"),
    ]


def test_parse_mountinfo_skips_malformed_lines():
    text = "\n".join(
        [
            "",
            "garbage",
            "1 2 3 4 5 6 ext4 /dev/x rw",  # no separator
            "36 35 98:0 - ext4 /dev/x rw",  # separator before the 6 mandatory fields
            "36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 -",  # nothing after the separator
            "36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 - ext4",  # no source
            "50 1 8:1 / /mnt/ok rw - ext4 /dev/sda1 rw",
        ]
    )
    assert rsm.parse_mountinfo(text) == [Mount("8:1", "/mnt/ok", "ext4", "/dev/sda1")]


def test_read_text_survives_non_utf8_mountpoints(tmp_path):
    p = tmp_path / "mountinfo"
    p.write_bytes(b"50 1 8:1 / /mnt/caf\xe9 rw - ext4 /dev/sda1 rw\n")
    (m,) = rsm.parse_mountinfo(rsm._read_text(str(p)))
    assert m.dev == "8:1"
    assert m.mountpoint.startswith("/mnt/caf")


# --------------------------------------------------------------------------
# 2. mount_for_path / mount_at / is_under
# --------------------------------------------------------------------------

ROOTFS = Mount(ROOT_FS_DEV, "/", "ext4", "/dev/nvme0n1p2")


def test_mount_for_path_longest_prefix_wins():
    mounts = [ROOTFS, Mount("0:50", "/mnt", "tmpfs", "tmpfs"), Mount(OLD_LOG_DEV, LOG, "ext4", "/dev/sdc1")]
    assert rsm.mount_for_path(mounts, LOG + "/prometheus/wal").dev == OLD_LOG_DEV
    assert rsm.mount_for_path(mounts, LOG).dev == OLD_LOG_DEV
    assert rsm.mount_for_path(mounts, "/mnt/other").dev == "0:50"


def test_mount_for_path_topmost_entry_wins_on_equal_mountpoints():
    old = Mount(OLD_LOG_DEV, LOG, "ext4", "/dev/sdc1")
    new = Mount(NEW_LOG_DEV, LOG + "/", "ext4", "/dev/sdf1")  # trailing slash must not matter
    assert rsm.mount_for_path([ROOTFS, old, new], LOG + "/x").dev == NEW_LOG_DEV
    assert rsm.mount_for_path([ROOTFS, new, old], LOG + "/x").dev == OLD_LOG_DEV
    assert rsm.mount_at([ROOTFS, old, new], LOG).dev == NEW_LOG_DEV


def test_mount_for_path_falls_back_to_root():
    mounts = [ROOTFS, Mount(OLD_LOG_DEV, LOG, "ext4", "/dev/sdc1")]
    assert rsm.mount_for_path(mounts, "/srv/data") == ROOTFS
    assert rsm.mount_for_path([], "/srv/data") is None


def test_is_under_respects_path_component_boundaries():
    assert not rsm.is_under("/mnt/logging", "/mnt/log")
    assert not rsm.is_under("/mnt/logging/x", "/mnt/log")
    assert rsm.is_under("/mnt/log/x", "/mnt/log")
    assert rsm.is_under("/mnt/log", "/mnt/log/")
    assert rsm.is_under("/anything", "/")
    mounts = [ROOTFS, Mount("8:1", "/mnt/log", "ext4", "/dev/sda1")]
    assert rsm.mount_for_path(mounts, "/mnt/logging/x") == ROOTFS


def test_mount_at_matches_exact_mountpoint_only():
    mounts = [ROOTFS, Mount("8:1", "/data", "ext4", "/dev/sda1")]
    assert rsm.mount_at(mounts, "/data/sub") is None
    assert rsm.mount_at(mounts, "/data/").dev == "8:1"


def test_watched_host_mounts_lists_only_mounts_under_roots(world):
    assert rsm.watched_host_mounts([ROOT]) == ((OLD_LOG_DEV, LOG), (DOCS_DEV, DOCS))
    assert rsm.watched_host_mounts([LOG]) == ((OLD_LOG_DEV, LOG),)


# --------------------------------------------------------------------------
# 3. scan()
# --------------------------------------------------------------------------


def _scan(world, roots=(ROOT,), reader=None, **kw):
    return rsm.scan(world.inspect_all(), world.host_mounts(), reader or world.read_mountinfo, list(roots), **kw)


def test_scan_flags_bind_on_a_different_device_than_the_host(world):
    cid = world.add("prometheus", [(LOG + "/prometheus", "/prometheus")])
    replug_log(world)
    s = _scan(world)
    assert s.stale_ids == {cid}
    (b,) = s.stale
    assert (b.name, b.source, b.destination, b.container_dev) == (
        "prometheus",
        LOG + "/prometheus",
        "/prometheus",
        OLD_LOG_DEV,
    )
    assert (b.host_mount.mountpoint, b.host_mount.dev, b.host_mount.source) == (LOG, NEW_LOG_DEV, "/dev/sdf1")
    assert s.blocked == {}


def test_scan_same_device_is_not_stale(world):
    world.add("prometheus", [(LOG + "/prometheus", "/prometheus")])
    s = _scan(world)
    assert s.stale == []
    assert s.blocked == {}
    assert s.notes == []


def test_scan_ignores_containers_that_are_not_running(world):
    world.add("stopped", [(LOG + "/a", "/a")], running=False)
    cid = world.add("no-pid", [(LOG + "/b", "/b")])
    world.containers[cid]["State"]["Pid"] = 0
    replug_log(world)

    def reader(pid):
        raise AssertionError("must not read the mountinfo of a non-running container")

    s = _scan(world, reader=reader)
    assert (s.stale, s.blocked, s.notes) == ([], {}, [])


def test_scan_ignores_binds_outside_the_watched_roots(world):
    cid = world.add("outside", [("/srv/jt-data", "/data"), ("/mntx/jt", "/x")])
    world.containers[cid]["Mounts"].append(
        {"Type": "volume", "Source": LOG + "/volume-ish", "Destination": "/vol", "RW": True}
    )
    replug_log(world)

    def reader(pid):
        raise AssertionError("a container without watched binds must not be inspected")

    s = _scan(world, reader=reader)
    assert (s.stale, s.blocked, s.notes) == ([], {}, [])


def test_scan_follows_symlinked_bind_sources(world):
    cid = world.add("linked", [("/srv/jt-link/prom", "/prometheus")])
    world.inner[world.pid(cid)]["/prometheus"] = OLD_LOG_DEV  # the link points into LOG
    links = {"/srv/jt-link/prom": LOG + "/prom"}

    def realpath(p):
        return links.get(p, p)

    assert _scan(world, realpath=realpath).stale == []
    replug_log(world)
    s = _scan(world, realpath=realpath)
    assert s.stale_ids == {cid}
    assert s.stale[0].source == LOG + "/prom"
    assert _scan(world).stale == []  # without resolving the link it is not watched at all


def test_scan_blocks_container_whose_disk_is_missing(world):
    cid = world.add("paperless-db", [(DOCS + "/pgdata", "/var/lib/postgresql/data")])
    world.unplug(DOCS)
    s = _scan(world)
    assert s.stale == []
    assert cid in s.blocked
    assert "resolves to /" in s.blocked[cid]
    assert any(n.startswith("paperless-db: ") and "not on a mounted disk" in n for n in s.notes)


@pytest.mark.parametrize("missing_first", [True, False])
def test_scan_stale_bind_plus_missing_disk_is_both_stale_and_blocked(world, missing_first):
    binds = [(LOG + "/app", "/data"), (DOCS + "/app", "/docs")]
    if missing_first:
        binds.reverse()
    cid = world.add("jt-app", binds)
    replug_log(world)
    world.unplug(DOCS)
    s = _scan(world)
    assert cid in s.stale_ids
    assert cid in s.blocked  # so fix() must not restart it


def test_scan_unreadable_container_mountinfo_is_noted_and_skipped(world):
    cid = world.add("locked", [(LOG + "/a", "/a")])
    replug_log(world)
    world.unreadable.add(world.pid(cid))
    s = _scan(world)
    assert s.stale == []
    assert s.blocked == {}
    assert s.notes == ["locked: cannot read its mountinfo (PermissionError), skipped"]


def test_scan_missing_destination_is_noted_and_skipped(world):
    cid = world.add("odd", [(LOG + "/a", "/a")])
    replug_log(world)
    del world.inner[world.pid(cid)]["/a"]
    s = _scan(world)
    assert s.stale == []
    assert s.notes == ["odd: no mount at /a inside the container, skipped"]


# --------------------------------------------------------------------------
# 4. restart_order()
# --------------------------------------------------------------------------


def _c(name, project="", service="", depends_on=""):
    labels = {}
    if project:
        labels["com.docker.compose.project"] = project
    if service:
        labels["com.docker.compose.service"] = service
    if depends_on:
        labels["com.docker.compose.depends_on"] = depends_on
    return {"Id": f"id-{name}", "Name": "/" + name, "Config": {"Labels": labels}}


def _names(containers):
    return [c["Name"].lstrip("/") for c in containers]


def test_restart_order_puts_compose_dependencies_first():
    cs = [
        _c("paperless-webserver", "paperless", "webserver", "db:service_started:false,broker:service_started:false"),
        _c("zz-paperless-db", "paperless", "db"),
        _c("paperless-broker", "paperless", "broker"),
    ]
    assert _names(rsm.restart_order(cs)) == ["paperless-broker", "zz-paperless-db", "paperless-webserver"]


def test_restart_order_follows_dependency_chains():
    cs = [
        _c("a", "p", "a", "b:service_healthy:true"),
        _c("b", "p", "b", "c:service_started:false"),
        _c("c", "p", "c"),
    ]
    assert _names(rsm.restart_order(cs)) == ["c", "b", "a"]


def test_restart_order_monitoring_first_among_ready_containers():
    cs = [
        _c("immich_server", "immich", "immich-server"),
        _c("prometheus", "monitoring", "prometheus"),
        _c("audiobookshelf", "abs", "audiobookshelf"),
        _c("loki", "monitoring", "loki"),
    ]
    assert _names(rsm.restart_order(cs)) == ["loki", "prometheus", "audiobookshelf", "immich_server"]


def test_restart_order_cycle_falls_back_deterministically():
    cs = [
        _c("svc-a", "p", "a", "b:service_started:false"),
        _c("svc-b", "p", "b", "a:service_started:false"),
        _c("svc-c", "p", "c"),
    ]
    orders = {tuple(_names(rsm.restart_order(list(perm)))) for perm in itertools.permutations(cs)}
    assert orders == {("svc-c", "svc-a", "svc-b")}


def test_restart_order_ignores_dependencies_in_other_projects():
    cross = [_c("a-web", "proj1", "web", "db:service_started:false"), _c("z-db", "proj2", "db")]
    assert _names(rsm.restart_order(cross)) == ["a-web", "z-db"]
    same = [_c("a-web", "proj1", "web", "db:service_started:false"), _c("z-db", "proj1", "db")]
    assert _names(rsm.restart_order(same)) == ["z-db", "a-web"]


def test_restart_order_ignores_unknown_and_self_dependencies():
    cs = [_c("a", "p", "a", "a:service_started:false,ghost:service_started:false,"), _c("b", "p", "b")]
    assert _names(rsm.restart_order(cs)) == ["a", "b"]


# --------------------------------------------------------------------------
# 5. restart_one()
# --------------------------------------------------------------------------


def test_restart_one_ok(world, stale):
    assert rsm.restart_one(stale, [ROOT]) == "ok"
    assert world.actions == [("stop", stale), ("start", stale)]
    assert world.inner[world.pid(stale)] == {"/data": NEW_LOG_DEV, "/docs": DOCS_DEV}
    assert world.clock.sleeps == [rsm.SETTLE_SECONDS]


def test_restart_one_waits_for_a_starting_healthcheck(world, stale):
    world.hooks[("start", stale)] = lambda c: world.health.__setitem__(stale, ["starting", "starting", "healthy"])
    assert rsm.restart_one(stale, [ROOT]) == "ok"
    assert world.clock.sleeps == [rsm.SETTLE_SECONDS, 2, 2]


def _gone(w, cid):
    w.halt(cid)
    del w.containers[cid]


def _restarted_elsewhere(w, cid):
    w.halt(cid)
    w.boot(cid)


@pytest.mark.parametrize(
    "make_ineligible",
    [
        pytest.param(_gone, id="gone"),
        pytest.param(lambda w, cid: w.halt(cid), id="not-running"),
        pytest.param(lambda w, cid: w.containers[cid]["State"].update(Paused=True), id="paused"),
        pytest.param(lambda w, cid: w.containers[cid]["HostConfig"].update(AutoRemove=True), id="auto-remove"),
        pytest.param(
            lambda w, cid: w.containers[cid]["Config"]["Labels"].update({"com.docker.compose.oneoff": "True"}),
            id="compose-oneoff",
        ),
        pytest.param(_restarted_elsewhere, id="no-longer-stale"),
        pytest.param(lambda w, cid: w.unplug(DOCS), id="blocked-other-disk-missing"),
    ],
)
def test_restart_one_skips_without_touching_the_container(world, stale, make_ineligible):
    make_ineligible(world, stale)
    assert rsm.restart_one(stale, [ROOT]) == "skipped"
    assert world.actions == []


@pytest.mark.parametrize(
    "during_stop",
    [
        pytest.param(lambda w: w.plug(LOG, "8:97", "/dev/sdg1"), id="disk-re-enumerated-again"),
        pytest.param(lambda w: w.unplug(LOG), id="stale-disk-vanished"),
        pytest.param(lambda w: w.unplug(DOCS), id="other-watched-disk-vanished"),
    ],
)
def test_restart_one_aborts_and_leaves_it_stopped_when_a_disk_changes_during_stop(world, stale, during_stop):
    world.hooks[("stop", stale)] = lambda c: during_stop(world)
    assert rsm.restart_one(stale, [ROOT]) == "abort"
    assert world.actions == [("stop", stale)]  # never started onto the wrong disk / root fs
    assert world.containers[stale]["State"]["Running"] is False


def _start_hook(fn):
    def setup(w, cid):
        w.hooks[("start", cid)] = lambda c: fn(w, cid, c)

    return setup


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(
            lambda w, cid: w.fail.__setitem__(("start", cid), "Error response from daemon: oci runtime error"),
            id="start-fails",
        ),
        pytest.param(_start_hook(lambda w, cid, c: w.halt(cid)), id="exited-after-start"),
        pytest.param(_start_hook(lambda w, cid, c: c["State"].update(Restarting=True)), id="restarting"),
        pytest.param(
            _start_hook(lambda w, cid, c: c.update(RestartCount=c["RestartCount"] + 1)), id="restart-count-up"
        ),
        pytest.param(_start_hook(lambda w, cid, c: w.health.__setitem__(cid, ["unhealthy"])), id="unhealthy"),
        pytest.param(
            _start_hook(lambda w, cid, c: w.inner[c["State"]["Pid"]].update({"/data": OLD_LOG_DEV})),
            id="still-stale",
        ),
        pytest.param(_start_hook(lambda w, cid, c: w.unplug(DOCS)), id="disk-missing-after-start"),
        pytest.param(_start_hook(lambda w, cid, c: _gone(w, cid)), id="vanished-after-start"),
    ],
)
def test_restart_one_reports_failed(world, stale, setup):
    setup(world, stale)
    assert rsm.restart_one(stale, [ROOT]) == "failed"
    assert world.actions == [("stop", stale), ("start", stale)]


def test_restart_one_detects_crash_restart_when_it_had_restarts_before(world, stale):
    """`docker start` resets RestartCount to 0, so a container with an old count of 3
    that crashes once after our start shows 1, which is still a crash."""
    world.containers[stale]["RestartCount"] = 3
    world.hooks[("start", stale)] = lambda c: c.update(RestartCount=c["RestartCount"] + 1)
    assert rsm.restart_one(stale, [ROOT]) == "failed"


def test_restart_one_failed_stop_is_not_reported_ok(world, stale):
    world.fail[("stop", stale)] = "Error response from daemon: cannot stop container"
    assert rsm.restart_one(stale, [ROOT]) == "failed"
    assert world.actions == [("stop", stale), ("start", stale)]


# --------------------------------------------------------------------------
# 6. fix()
# --------------------------------------------------------------------------


def _run(world, deadline_in=900.0, failed=None):
    return rsm.Run(roots=[ROOT], deadline=world.clock.mono + deadline_in, failed=dict(failed or {}))


@pytest.fixture
def two_stale(world):
    a = world.add("a-first", [(LOG + "/a", "/a")])
    b = world.add("b-second", [(LOG + "/b", "/b")])
    replug_log(world)
    return a, b


def _recording_restart(world, monkeypatch, outcome):
    """Replace restart_one; `outcome(cid)` returns the result (or raises). "ok" really fixes it."""
    seen = []

    def fake(cid, roots):
        seen.append(cid)
        result = outcome(cid)
        if result == "ok":
            _restarted_elsewhere(world, cid)
        return result

    monkeypatch.setattr(rsm, "restart_one", fake)
    return seen


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(["docker", "stop", "x"], 180),
        OSError(5, "Input/output error"),
        ValueError("bad json"),
        RuntimeError("boom"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_fix_exception_in_one_restart_does_not_stop_the_queue(world, two_stale, monkeypatch, exc):
    a, b = two_stale

    def outcome(cid):
        if cid == a:
            raise exc
        return "ok"

    seen = _recording_restart(world, monkeypatch, outcome)
    run = _run(world)
    rsm.fix(run)
    assert seen == [a, b]
    assert run.failed == {a: "a-first"}
    assert run.restarted_ok == 1
    assert run.fixed_since_scan == {b}


def test_fix_timeout_from_real_docker_call_does_not_stop_the_queue(world, two_stale):
    a, b = two_stale
    world.fail[("stop", a)] = subprocess.TimeoutExpired(["docker", "stop", a], 180)
    run = _run(world)
    rsm.fix(run)
    # a's stop was attempted (and timed out); b still got its full restart
    assert world.actions == [("stop", a), ("stop", b), ("start", b)]
    assert run.failed == {a: "a-first"}
    assert run.restarted_ok == 1


def test_fix_abort_stops_the_queue(world, two_stale, monkeypatch):
    a, b = two_stale
    seen = _recording_restart(world, monkeypatch, lambda cid: "abort")
    run = _run(world)
    rsm.fix(run)
    assert seen == [a]
    assert run.failed == {a: "a-first"}
    assert run.restarted_ok == 0


def test_fix_abort_from_a_disk_vanishing_mid_run(world, two_stale):
    a, b = two_stale
    world.hooks[("stop", a)] = lambda c: world.unplug(LOG)
    run = _run(world)
    rsm.fix(run)
    assert world.actions == [("stop", a)]
    assert run.failed == {a: "a-first"}


def test_fix_skipped_is_neither_ok_nor_failed(world, two_stale, monkeypatch):
    a, b = two_stale
    _recording_restart(world, monkeypatch, lambda cid: "skipped")
    run = _run(world, failed={"old-id": "old"})
    rsm.fix(run)
    assert run.failed == {"old-id": "old"}
    assert run.restarted_ok == 0


def test_fix_single_pass_when_mounts_unchanged(world, two_stale, monkeypatch):
    real = rsm.running_containers
    listings = []
    monkeypatch.setattr(rsm, "running_containers", lambda: listings.append(1) or real())
    run = _run(world)
    rsm.fix(run)
    assert len(listings) == 1
    a, b = two_stale
    assert world.actions == [("stop", a), ("start", a), ("stop", b), ("start", b)]
    assert run.restarted_ok == 2


def test_fix_second_pass_when_a_late_disk_is_mounted(world):
    """Pass 1: B's disk is missing (blocked). A parallel `mount -a` mounts it while A
    restarts, so pass 2 finds B on the bare root-fs directory and moves it."""
    a = world.add("a-prom", [(LOG + "/prom", "/prometheus")])
    world.unplug(DOCS)
    b = world.add("b-paperless-db", [(DOCS + "/pg", "/var/lib/postgresql/data")])
    assert world.inner[world.pid(b)] == {"/var/lib/postgresql/data": ROOT_FS_DEV}
    replug_log(world)
    world.hooks[("start", a)] = lambda c: world.plug(DOCS, DOCS_DEV, "/dev/sdd1")
    run = _run(world)
    rsm.fix(run)
    assert world.actions == [("stop", a), ("start", a), ("stop", b), ("start", b)]
    assert world.inner[world.pid(b)] == {"/var/lib/postgresql/data": DOCS_DEV}
    assert run.restarted_ok == 2
    assert run.failed == {}


def test_fix_at_most_max_passes(world, two_stale, monkeypatch):
    a, b = two_stale
    counter = itertools.count()
    monkeypatch.setattr(rsm, "watched_host_mounts", lambda roots: (("dev", str(next(counter))),))
    seen = _recording_restart(world, monkeypatch, lambda cid: "failed")
    run = _run(world)
    rsm.fix(run)
    assert seen == [a, b] * rsm.MAX_PASSES
    assert rsm.MAX_PASSES == 3


def test_fix_deadline_stops_new_restarts(world, two_stale, monkeypatch):
    a, b = two_stale

    def slow(cid):
        world.clock.mono += 150
        return "ok"

    seen = _recording_restart(world, monkeypatch, slow)
    run = _run(world, deadline_in=100)
    rsm.fix(run)
    assert seen == [a]
    assert run.restarted_ok == 1
    assert run.failed == {}  # b was never touched: it is still stale, not failed


def test_fix_deadline_already_passed_restarts_nothing(world, two_stale, monkeypatch):
    seen = _recording_restart(world, monkeypatch, lambda cid: "ok")
    rsm.fix(_run(world, deadline_in=-1))
    assert seen == []


def test_fix_never_restarts_blocked_containers(world):
    cid = world.add("jt-app", [(LOG + "/app", "/data"), (DOCS + "/app", "/docs")])
    replug_log(world)
    world.unplug(DOCS)
    run = _run(world)
    rsm.fix(run)
    assert world.actions == []
    assert run.failed == {}


# --------------------------------------------------------------------------
# 7. main()
# --------------------------------------------------------------------------


def test_main_installs_sigterm_handler_that_unwinds(world, mdir):
    assert main("--metrics-dir", mdir) == 0
    assert world.signals == [(signal.SIGTERM, rsm._terminate)]
    with pytest.raises(SystemExit) as ei:
        rsm._terminate(signal.SIGTERM, None)
    assert ei.value.code == 143


@pytest.mark.parametrize("fix_mode", [False, True])
def test_main_docker_inactive_leaves_metrics_untouched(world, mdir, state_file, monkeypatch, fix_mode):
    monkeypatch.setattr(rsm, "_dockerd_inactive", lambda: True)
    files = {mdir / CHECK_PROM: b"old check\n", mdir / RESTART_PROM: b"old restart\n", state_file: b'{"failed": {}}\n'}
    for path, content in files.items():
        path.write_bytes(content)
    inodes = {path: path.stat().st_ino for path in files}
    args = ["--metrics-dir", mdir, "--state-file", state_file] + (["--fix"] if fix_mode else [])
    assert main(*args) == 0
    for path, content in files.items():
        assert path.read_bytes() == content
        assert path.stat().st_ino == inodes[path]
    assert world.calls == []  # not even `docker ps` (would socket-activate dockerd)


def test_main_daemon_unreachable_leaves_metrics_untouched(world, mdir):
    world.fail[("ps", "")] = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
    (mdir / CHECK_PROM).write_text("old\n")
    assert main("--metrics-dir", mdir) == 0
    assert (mdir / CHECK_PROM).read_text() == "old\n"


def test_main_check_mode_reports_and_never_restarts(world, stale, mdir, state_file):
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 1
    assert read_prom(mdir / CHECK_PROM) == {
        "jess_stale_mount_containers": 1,
        "jess_stale_mount_restart_failures": 0,
        "jess_stale_mount_check_last_run_timestamp_seconds": WALL,
    }
    assert stat.S_IMODE((mdir / CHECK_PROM).stat().st_mode) == 0o644
    assert world.actions == []
    assert not (mdir / RESTART_PROM).exists()
    assert not state_file.exists()  # check runs never write the state file


def test_main_check_mode_nothing_stale(world, mdir):
    world.add("healthy", [(LOG + "/a", "/a")])
    world.add("no-binds", [])
    assert main("--metrics-dir", mdir) == 0
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_containers"] == 0
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 0


def test_main_fix_one_success_writes_restart_metrics_and_state(world, stale, mdir, state_file):
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 0
    assert world.actions == [("stop", stale), ("start", stale)]
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_containers"] == 0
    assert read_prom(mdir / RESTART_PROM) == {
        "jess_stale_mount_restarted_containers": 1,
        "jess_stale_mount_last_restart_timestamp_seconds": WALL,
    }
    assert json.loads(state_file.read_text()) == {"failed": {}}
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((mdir / RESTART_PROM).stat().st_mode) == 0o644


def test_main_fix_restarts_monitoring_first(world, mdir):
    immich = world.add("immich_postgres", [(LOG + "/pg", "/data")], project="immich", service="database")
    prom = world.add("prometheus", [(LOG + "/prom", "/prometheus")], project="monitoring", service="prometheus")
    replug_log(world)
    assert main("--fix", "--metrics-dir", mdir) == 0
    assert world.actions == [("stop", prom), ("start", prom), ("stop", immich), ("start", immich)]
    assert read_prom(mdir / RESTART_PROM)["jess_stale_mount_restarted_containers"] == 2


@pytest.mark.parametrize("pre_existing", [True, False])
def test_main_fix_without_success_leaves_restart_metrics_alone(world, stale, mdir, state_file, pre_existing):
    if pre_existing:
        (mdir / RESTART_PROM).write_text("previous restart\n")
    world.fail[("start", stale)] = "Error response from daemon: no space left"
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 1
    if pre_existing:
        assert (mdir / RESTART_PROM).read_text() == "previous restart\n"
    else:
        assert not (mdir / RESTART_PROM).exists()
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 1
    assert json.loads(state_file.read_text()) == {"failed": {stale: "jt-app"}}


def test_main_fix_paused_container_is_reported_not_restarted(world, stale, mdir, state_file):
    world.containers[stale]["State"]["Paused"] = True
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 1
    assert world.actions == []
    prom = read_prom(mdir / CHECK_PROM)
    assert (prom["jess_stale_mount_containers"], prom["jess_stale_mount_restart_failures"]) == (1, 0)


def test_main_fix_never_starts_a_stopped_container(world, mdir):
    cid = world.add("deliberately-stopped", [(LOG + "/x", "/x")], running=False)
    replug_log(world)
    assert main("--fix", "--metrics-dir", mdir) == 0
    assert world.actions == []
    assert world.containers[cid]["State"]["Running"] is False


def test_main_failures_persist_until_the_container_runs_on_the_live_disk(world, stale, mdir, state_file):
    world.fail[("start", stale)] = "Error response from daemon: boom"
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 1
    assert json.loads(state_file.read_text())["failed"] == {stale: "jt-app"}
    assert world.containers[stale]["State"]["Running"] is False
    del world.fail[("start", stale)]

    # a later check: still stopped -> still a failure (the stale count cannot see it)
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 1
    prom = read_prom(mdir / CHECK_PROM)
    assert (prom["jess_stale_mount_containers"], prom["jess_stale_mount_restart_failures"]) == (0, 1)

    # fixed by hand on the live disk -> the next check clears it by itself
    world.boot(stale)
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 0
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 0
    assert world.actions == [("stop", stale), ("start", stale)]  # the checks restarted nothing


def test_main_failure_still_counted_while_running_stale(world, stale, mdir, state_file):
    state_file.write_text(json.dumps({"failed": {stale: "jt-app"}}))
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 1
    prom = read_prom(mdir / CHECK_PROM)
    assert (prom["jess_stale_mount_containers"], prom["jess_stale_mount_restart_failures"]) == (1, 1)


def test_main_failure_cleared_when_container_no_longer_exists(world, mdir, state_file):
    state_file.write_text(json.dumps({"failed": {"f" * 64: "removed-one"}}))
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 0
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 0


def test_main_failure_cleared_when_inspect_one_returns_none(world, mdir, state_file, monkeypatch):
    state_file.write_text(json.dumps({"failed": {"e" * 64: "recreated"}}))
    monkeypatch.setattr(rsm, "inspect_one", lambda cid: None)
    assert main("--metrics-dir", mdir, "--state-file", state_file) == 0
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 0


def test_main_fix_clears_a_recorded_failure_it_fixes(world, stale, mdir, state_file):
    state_file.write_text(json.dumps({"failed": {stale: "jt-app", "f" * 64: "gone"}}))
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 0
    assert json.loads(state_file.read_text()) == {"failed": {}}


def test_main_abort_leaves_container_stopped_and_counted(world, stale, mdir, state_file):
    world.hooks[("stop", stale)] = lambda c: world.unplug(LOG)
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 1
    assert world.actions == [("stop", stale)]
    assert json.loads(state_file.read_text())["failed"] == {stale: "jt-app"}
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_restart_failures"] == 1


def test_main_final_recheck_failure_reports_last_known_state(world, two_stale, mdir, state_file, monkeypatch):
    real = rsm.running_containers
    calls = itertools.count(1)

    def flaky():
        if next(calls) == 3:  # main's listing, fix pass 1, then the final re-check
            raise RuntimeError("docker ps failed: daemon hiccup")
        return real()

    monkeypatch.setattr(rsm, "running_containers", flaky)
    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 0
    prom = read_prom(mdir / CHECK_PROM)
    assert (prom["jess_stale_mount_containers"], prom["jess_stale_mount_restart_failures"]) == (0, 0)
    assert read_prom(mdir / RESTART_PROM)["jess_stale_mount_restarted_containers"] == 2


def test_main_sigterm_mid_fix_still_writes_metrics_and_state(world, two_stale, mdir, state_file):
    a, b = two_stale

    def sigterm(c):
        raise SystemExit(143)

    world.hooks[("stop", b)] = sigterm  # systemd TimeoutStartSec fires during b's restart
    with pytest.raises(SystemExit) as ei:
        main("--fix", "--metrics-dir", mdir, "--state-file", state_file)
    assert ei.value.code == 143
    assert read_prom(mdir / CHECK_PROM)["jess_stale_mount_containers"] == 1  # b, a was fixed
    assert read_prom(mdir / RESTART_PROM)["jess_stale_mount_restarted_containers"] == 1
    assert isinstance(json.loads(state_file.read_text())["failed"], dict)
    assert tmp_leftovers(mdir, state_file.parent) == []
    with os.scandir(mdir) as it:
        assert sorted(e.name for e in it) == [CHECK_PROM, RESTART_PROM]


# Regression: a SIGTERM between `docker stop` and `docker start` must leave the
# stopped container in the state file and jess_stale_mount_restart_failures.
def test_main_sigterm_between_stop_and_start_records_the_stopped_container(world, two_stale, mdir, state_file):
    a, b = two_stale

    def sigterm(c):
        raise SystemExit(143)

    world.hooks[("stop", b)] = sigterm
    with pytest.raises(SystemExit):
        main("--fix", "--metrics-dir", mdir, "--state-file", state_file)
    assert world.containers[b]["State"]["Running"] is False  # left stopped
    del world.hooks[("stop", b)]
    # 10 minutes later the check timer runs: b must still be reported somewhere
    rc = main("--metrics-dir", mdir, "--state-file", state_file)
    prom = read_prom(mdir / CHECK_PROM)
    assert prom["jess_stale_mount_containers"] + prom["jess_stale_mount_restart_failures"] >= 1
    assert rc == 1


def test_main_check_skips_while_a_fix_run_holds_the_lock(world, stale, mdir):
    (mdir / CHECK_PROM).write_text("from the fix run\n")
    fd = os.open(mdir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert main("--metrics-dir", mdir) == 0
    finally:
        os.close(fd)
    assert (mdir / CHECK_PROM).read_text() == "from the fix run\n"
    assert world.calls == []


def test_main_releases_the_lock(world, mdir):
    assert main("--fix", "--metrics-dir", mdir) == 0
    fd = os.open(mdir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if still held
    finally:
        os.close(fd)


def test_main_exit_codes(world, mdir, monkeypatch):
    world.add("healthy", [(LOG + "/a", "/a")])
    assert main("--metrics-dir", mdir) == 0
    world.add("stale-one", [(LOG + "/b", "/b")])
    replug_log(world)
    assert main("--metrics-dir", mdir) == 1


def test_main_exit_2_when_containers_cannot_be_listed(world, mdir, monkeypatch):
    def broken():
        raise RuntimeError("docker ps failed: permission denied")

    monkeypatch.setattr(rsm, "running_containers", broken)
    assert main("--metrics-dir", mdir) == 2
    assert not (mdir / CHECK_PROM).exists()


def test_main_exit_2_when_docker_ps_fails(world, mdir):
    world.fail[("ps", "")] = "permission denied while trying to connect"
    assert main("--metrics-dir", mdir) == 2


# --------------------------------------------------------------------------
# running_containers / _dockerd_inactive
# --------------------------------------------------------------------------


def test_running_containers_never_calls_docker_when_dockerd_inactive(world, monkeypatch):
    monkeypatch.setattr(rsm, "_dockerd_inactive", lambda: True)
    with pytest.raises(rsm.DockerUnavailable):
        rsm.running_containers()
    assert world.calls == []


def test_running_containers_docker_cli_missing(world):
    world.fail[("ps", "")] = FileNotFoundError(2, "docker")
    with pytest.raises(rsm.DockerUnavailable):
        rsm.running_containers()


def test_running_containers_empty(world):
    assert rsm.running_containers() == []
    assert world.calls == [("ps", "-q", "--no-trunc")]


def test_running_containers_container_exits_between_ps_and_inspect(world):
    keep = world.add("keep", [])
    leave = world.add("leave", [])
    world.hooks[("ps", "")] = lambda _: _gone(world, leave)
    assert [c["Id"] for c in rsm.running_containers()] == [keep]


@pytest.mark.parametrize(
    "stdout, inactive",
    [("active\n", False), ("reloading\n", False), ("inactive\n", True), ("activating\n", True), ("failed\n", True)],
)
def test_dockerd_inactive_asks_systemd(monkeypatch, stdout, inactive):
    monkeypatch.setattr(rsm, "Path", lambda p: SimpleNamespace(is_dir=lambda: True))
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(rsm.subprocess, "run", fake_run)
    assert rsm._dockerd_inactive() is inactive
    assert seen == [["systemctl", "is-active", "docker.service"]]


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "systemctl"), subprocess.TimeoutExpired(["systemctl"], 30)])
def test_dockerd_inactive_falls_back_to_docker_cli(monkeypatch, exc):
    monkeypatch.setattr(rsm, "Path", lambda p: SimpleNamespace(is_dir=lambda: True))

    def fake_run(cmd, **kw):
        raise exc

    monkeypatch.setattr(rsm.subprocess, "run", fake_run)
    assert rsm._dockerd_inactive() is False


def test_dockerd_inactive_without_systemd(monkeypatch):
    monkeypatch.setattr(rsm, "Path", lambda p: SimpleNamespace(is_dir=lambda: False))

    def fake_run(cmd, **kw):
        raise AssertionError("must not call systemctl without systemd")

    monkeypatch.setattr(rsm.subprocess, "run", fake_run)
    assert rsm._dockerd_inactive() is False


# --------------------------------------------------------------------------
# 8. Security: _write_atomic and load_failed
# --------------------------------------------------------------------------


def test_write_atomic_does_not_follow_planted_symlinks(tmp_path):
    d = tmp_path / "textfile_collector"
    d.mkdir()
    victim_tmp, victim_target = tmp_path / "victim-tmp", tmp_path / "victim-target"
    victim_tmp.write_text("precious 1\n")
    victim_target.write_text("precious 2\n")
    target = d / CHECK_PROM
    (d / (CHECK_PROM + ".tmp")).symlink_to(victim_tmp)  # the old fixed temp name
    target.symlink_to(victim_target)

    rsm._write_atomic(target, "jess_stale_mount_containers 0\n", 0o644)

    assert victim_tmp.read_text() == "precious 1\n"
    assert victim_target.read_text() == "precious 2\n"
    st = os.lstat(target)
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o644
    assert target.read_text() == "jess_stale_mount_containers 0\n"
    assert (d / (CHECK_PROM + ".tmp")).is_symlink()  # left alone, not written through
    assert tmp_leftovers(d) == []


def test_write_atomic_mode_ignores_umask(tmp_path):
    old = os.umask(0o077)
    try:
        rsm._write_atomic(tmp_path / "a.prom", "x\n", 0o644)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "a.prom").stat().st_mode) == 0o644


def test_write_atomic_removes_its_temp_file_when_interrupted(tmp_path, monkeypatch):
    target = tmp_path / CHECK_PROM
    target.write_text("old\n")

    def interrupted(src, dst):
        raise SystemExit(143)

    monkeypatch.setattr(rsm.os, "replace", interrupted)
    with pytest.raises(SystemExit):
        rsm._write_atomic(target, "new\n", 0o644)
    monkeypatch.undo()
    assert target.read_text() == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == [CHECK_PROM]


def test_main_does_not_follow_symlinks_planted_in_the_metrics_dir(world, stale, mdir, state_file, tmp_path):
    victims = {}
    for i, link in enumerate(
        [
            mdir / CHECK_PROM,
            mdir / (CHECK_PROM + ".tmp"),
            mdir / RESTART_PROM,
            mdir / (RESTART_PROM + ".tmp"),
            state_file,
            state_file.with_name(state_file.name + ".tmp"),
        ]
    ):
        victim = tmp_path / f"victim-{i}"
        victim.write_text(f"precious {i}\n")
        link.symlink_to(victim)
        victims[victim] = f"precious {i}\n"

    assert main("--fix", "--metrics-dir", mdir, "--state-file", state_file) == 0

    for victim, content in victims.items():
        assert victim.read_text() == content
    for path, mode in [(mdir / CHECK_PROM, 0o644), (mdir / RESTART_PROM, 0o644), (state_file, 0o600)]:
        st = os.lstat(path)
        assert stat.S_ISREG(st.st_mode), path
        assert stat.S_IMODE(st.st_mode) == mode, path


def test_load_failed_reads_a_regular_state_file(tmp_path):
    p = tmp_path / "failed.json"
    p.write_text(json.dumps({"failed": {"abc": "paperless-db", "n": 1}}))
    assert rsm.load_failed(p) == {"abc": "paperless-db", "n": "1"}


def test_load_failed_refuses_a_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"failed": {"abc": "paperless-db"}}))
    link = tmp_path / "failed.json"
    link.symlink_to(real)
    assert rsm.load_failed(link) == {}


def test_load_failed_refuses_a_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "failed.json"
    os.mkfifo(fifo)
    result = {}
    t = threading.Thread(target=lambda: result.update(value=rsm.load_failed(fifo)), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "load_failed blocked opening a FIFO"
    assert result["value"] == {}


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"\xff\xfe garbage",
        b"[1, 2]",
        b'"failed"',
        b"3",
        b"null",
        b'{"failed": ["abc"]}',
        b'{"failed": null}',
        b"",
    ],
)
def test_load_failed_bad_content_is_empty(tmp_path, content):
    p = tmp_path / "failed.json"
    p.write_bytes(content)
    assert rsm.load_failed(p) == {}


def test_load_failed_missing_none_or_directory(tmp_path):
    assert rsm.load_failed(None) == {}
    assert rsm.load_failed(tmp_path / "absent.json") == {}
    assert rsm.load_failed(tmp_path) == {}
