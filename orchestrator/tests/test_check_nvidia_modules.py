"""
Tests for scripts/check_nvidia_modules.py — the host-side warning that the next
reboot would boot a kernel with no NVIDIA module (Helios 2026-10-15, Saturn
2026-11-07, Jupiter 2026-11-21: unattended-upgrades installed a kernel and held
the NVIDIA module package back).

No root and no real /boot or /lib/modules: every test builds a fake boot dir
and modules dir under tmp_path and drives the script through its hidden
--boot-dir / --modules-dir / --running arguments. stdlib-only, so this runs in
CI and in the throwaway test container (which mounts scripts/).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.tests._helpers import REPO_ROOT, load_script

cnm = load_script(REPO_ROOT / "scripts" / "check_nvidia_modules.py", "check_nvidia_modules_under_test")

PROM = "jess_nvidia_modules.prom"
NOW = 1_800_000_000.7
K99 = "6.8.0-99-generic"
K149 = "6.8.0-149-generic"
K151 = "6.8.0-151-generic"
K1015 = "6.8.0-1015-generic"


# --------------------------------------------------------------------------
# Fixture helpers
# --------------------------------------------------------------------------


class Host:
    """A fake /boot + /lib/modules + node_exporter textfile dir under tmp_path."""

    def __init__(self, root: Path):
        self.root = root
        self.boot = root / "boot"
        self.modules = root / "lib-modules"
        self.metrics = root / "textfile"
        for d in (self.boot, self.modules, self.metrics):
            d.mkdir()

    def kernel(self, release: str, module: str | None = "kernel/nvidia-580-open/nvidia.ko") -> None:
        """Install a kernel image, and (unless module=None) an NVIDIA module for it."""
        (self.boot / f"vmlinuz-{release}").write_bytes(b"kernel image")
        (self.modules / release / "kernel" / "drivers").mkdir(parents=True, exist_ok=True)
        if module:
            self.module(release, module)

    def module(self, release: str, relpath: str) -> Path:
        path = self.modules / release / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"module")
        return path

    def args(self, running: str, metrics: bool = True, *extra: str) -> list[str]:
        argv = ["--boot-dir", str(self.boot), "--modules-dir", str(self.modules), "--running", running]
        if metrics:
            argv += ["--metrics-dir", str(self.metrics)]
        return argv + list(extra)

    def prom(self) -> str:
        return (self.metrics / PROM).read_text()

    def tree(self) -> set[str]:
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*")}


@pytest.fixture
def host(tmp_path, monkeypatch):
    # Fixed clock for the timestamp row; patch the script's own `time`, not the global module.
    monkeypatch.setattr(cnm, "time", SimpleNamespace(time=lambda: NOW))
    return Host(tmp_path)


def _rows(text: str) -> dict[tuple[str, str], int]:
    """{(kernel, boot): value} from the jess_nvidia_module_present rows."""
    out = {}
    for line in text.splitlines():
        if line.startswith("jess_nvidia_module_present{"):
            labels, value = line[len("jess_nvidia_module_present{") :].rsplit("} ", 1)
            parts = dict(kv.split("=", 1) for kv in labels.split(","))
            out[(parts["kernel"].strip('"'), parts["boot"].strip('"'))] = int(value)
    return out


# --------------------------------------------------------------------------
# Kernel discovery and ordering
# --------------------------------------------------------------------------


def test_kernels_sort_numerically_and_the_newest_is_the_next_boot_row(host):
    for release in (K151, K1015, K99, K149):  # deliberately out of order
        host.kernel(release)
    assert cnm.installed_kernels(host.boot) == [K99, K149, K151, K1015]
    rows = cnm.check(host.boot, host.modules, K149)
    assert rows == [(K149, "running", True), (K1015, "next", True)]


def test_minor_version_outranks_abi_number(host):
    # HWE kernels: 6.11.0-29 is newer than 6.8.0-200 although "6.11" < "6.8" as text.
    host.kernel("6.8.0-200-generic")
    host.kernel("6.11.0-29-generic")
    assert cnm.installed_kernels(host.boot)[-1] == "6.11.0-29-generic"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("vmlinuz", id="bare-vmlinuz"),
        pytest.param("vmlinuz.old", id="vmlinuz-old"),
        pytest.param("vmlinuz-6.8.0-160-generic.efi.signed", id="efi-signed"),
        pytest.param("vmlinuz-6.8.0-170-generic.dpkg-tmp", id="dpkg-tmp"),
        pytest.param("vmlinuz-6.8.0-180", id="no-flavour"),
        pytest.param("initrd.img-6.8.0-190-generic", id="initrd"),
        pytest.param("config-6.8.0-190-generic", id="config"),
        pytest.param("System.map-6.8.0-190-generic", id="system-map"),
    ],
)
def test_non_kernel_names_never_pose_as_the_next_kernel(host, name):
    host.kernel(K151)
    # Every decoy carries a HIGHER version than the real kernel, so counting it would change "next".
    (host.boot / name).write_bytes(b"not a kernel")
    assert cnm.installed_kernels(host.boot) == [K151]
    assert cnm.check(host.boot, host.modules, K151)[1] == (K151, "next", True)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("vmlinuz", id="vmlinuz"),
        pytest.param("vmlinuz.old", id="vmlinuz-old"),
        pytest.param("vmlinuz-6.8.0-152-generic", id="versioned-symlink"),
    ],
)
def test_symlinks_are_skipped(host, name):
    host.kernel(K151)
    target = host.root / "elsewhere" / "vmlinuz-6.8.0-152-generic"
    target.parent.mkdir()
    target.write_bytes(b"kernel image")
    (host.boot / name).symlink_to(target)
    assert cnm.installed_kernels(host.boot) == [K151]


@pytest.mark.parametrize(
    "release",
    [
        pytest.param("6.8.0-151-lowlatency", id="lowlatency"),
        pytest.param("6.8.0-151-generic-64k", id="generic-64k"),
        pytest.param("6.8.0-1015-oem", id="oem"),
    ],
)
def test_flavours_are_accepted(host, release):
    host.kernel(release)
    assert cnm.installed_kernels(host.boot) == [release]


def test_missing_boot_dir_means_no_kernels(tmp_path):
    assert cnm.installed_kernels(tmp_path / "no-such-boot") == []


# --------------------------------------------------------------------------
# Module detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["nvidia.ko", "nvidia.ko.zst", "nvidia.ko.xz", "nvidia.ko.gz"])
@pytest.mark.parametrize(
    "subdir",
    [
        pytest.param("kernel/nvidia-580-open", id="prebuilt"),
        pytest.param("updates/dkms", id="dkms"),
        pytest.param("", id="top-level"),
        pytest.param("a/b/c/d", id="deep"),
    ],
)
def test_module_found_in_any_subdir_any_compression(host, subdir, filename):
    host.kernel(K151, module=None)
    host.module(K151, f"{subdir}/{filename}" if subdir else filename)
    assert cnm.module_present(host.modules, K151) is True


@pytest.mark.parametrize(
    "filename",
    ["nvidia-drm.ko", "nvidia-uvm.ko.zst", "nvidia-modeset.ko", "nvidia.ko.bak", "nvidia.ko.sig", "nvidia"],
)
def test_other_files_do_not_count_as_the_module(host, filename):
    host.kernel(K151, module=None)
    host.module(K151, f"kernel/nvidia-580-open/{filename}")
    assert cnm.module_present(host.modules, K151) is False


def test_directory_named_like_the_module_does_not_count(host):
    host.kernel(K151, module=None)
    (host.modules / K151 / "kernel" / "nvidia.ko").mkdir(parents=True)
    assert cnm.module_present(host.modules, K151) is False


def test_module_of_another_kernel_does_not_count(host):
    host.kernel(K149)
    host.kernel(K151, module=None)
    assert cnm.module_present(host.modules, K149) is True
    assert cnm.module_present(host.modules, K151) is False


def test_missing_modules_dir_for_a_kernel_is_missing(host):
    (host.boot / f"vmlinuz-{K151}").write_bytes(b"kernel image")  # no /lib/modules/<k> at all
    assert cnm.module_present(host.modules, K151) is False


def test_modules_path_that_is_a_file_is_missing(host):
    (host.modules / K151).write_bytes(b"not a directory")
    assert cnm.module_present(host.modules, K151) is False


# --------------------------------------------------------------------------
# main(): exit codes and output
# --------------------------------------------------------------------------


def test_exit_0_when_running_and_next_have_modules(host, capsys):
    host.kernel(K149, "updates/dkms/nvidia.ko.zst")
    host.kernel(K151)
    assert cnm.main(host.args(K149)) == 0
    out = capsys.readouterr()
    assert f"running {K149}: NVIDIA module present" in out.out
    assert f"next    {K151}: NVIDIA module present" in out.out
    assert out.err == ""
    assert _rows(host.prom()) == {(K149, "running"): 1, (K151, "next"): 1}


def test_running_is_newest_gives_two_rows_for_one_kernel(host):
    host.kernel(K151)
    assert cnm.main(host.args(K151)) == 0
    assert _rows(host.prom()) == {(K151, "running"): 1, (K151, "next"): 1}


def test_exit_1_when_next_kernel_has_no_module(host, capsys):
    # The incident: a new kernel installed, its NVIDIA module package held back.
    host.kernel(K149)
    host.kernel(K151, module=None)
    assert cnm.main(host.args(K149)) == 1
    out = capsys.readouterr()
    assert f"next    {K151}: NVIDIA module MISSING" in out.out
    assert f"no NVIDIA module for {K151}." in out.err
    assert "sudo apt full-upgrade" in out.err
    # The metric is what drives NvidiaModuleMissing: it must be written on exit 1 too.
    assert _rows(host.prom()) == {(K149, "running"): 1, (K151, "next"): 0}


def test_exit_1_when_running_kernel_has_no_module(host, capsys):
    host.kernel(K149, module=None)
    host.kernel(K151)
    assert cnm.main(host.args(K149)) == 1
    assert _rows(host.prom()) == {(K149, "running"): 0, (K151, "next"): 1}
    assert f"no NVIDIA module for {K149}." in capsys.readouterr().err


def test_exit_1_running_kernel_not_in_boot_and_not_in_modules(host):
    host.kernel(K151)
    assert cnm.main(host.args("6.8.0-140-generic")) == 1
    assert _rows(host.prom()) == {("6.8.0-140-generic", "running"): 0, (K151, "next"): 1}


def test_exit_1_names_a_kernel_once_when_both_rows_miss(host, capsys):
    host.kernel(K151, module=None)
    assert cnm.main(host.args(K151)) == 1
    err = capsys.readouterr().err
    assert err.count(K151) == 1
    assert _rows(host.prom()) == {(K151, "running"): 0, (K151, "next"): 0}


def test_exit_1_lists_both_kernels_when_both_miss(host, capsys):
    host.kernel(K149, module=None)
    host.kernel(K151, module=None)
    assert cnm.main(host.args(K149)) == 1
    assert f"no NVIDIA module for {K149}, {K151}." in capsys.readouterr().err


@pytest.mark.parametrize(
    "decoys",
    [
        pytest.param([], id="empty"),
        pytest.param(["vmlinuz", "vmlinuz.old", "vmlinuz-6.8.0-151-generic.efi.signed"], id="only-decoys"),
        pytest.param(None, id="boot-dir-missing"),
    ],
)
def test_exit_2_when_no_kernel_is_installed(host, capsys, decoys):
    argv = host.args(K151)
    if decoys is None:
        argv[argv.index("--boot-dir") + 1] = str(host.root / "no-such-boot")
    for name in decoys or ():
        (host.boot / name).write_bytes(b"not a kernel")
    assert cnm.main(argv) == 2
    assert "check failed: no kernel images found" in capsys.readouterr().err
    assert not (host.metrics / PROM).exists()


@pytest.mark.parametrize("kind", ["missing", "is-a-file"])
def test_exit_2_when_metrics_dir_is_unusable(host, capsys, kind):
    host.kernel(K151)
    bad = host.root / "bad-metrics"
    if kind == "is-a-file":
        bad.write_text("x")
    argv = host.args(K151, False) + ["--metrics-dir", str(bad)]
    assert cnm.main(argv) == 2
    captured = capsys.readouterr()
    assert "check failed" in captured.err
    assert captured.out == ""  # no "present" lines after a failed check


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_exit_2_when_metrics_dir_is_read_only(host):
    host.kernel(K151)
    host.metrics.chmod(0o555)
    try:
        assert cnm.main(host.args(K151)) == 2
        assert list(host.metrics.iterdir()) == []
    finally:
        host.metrics.chmod(0o755)


@pytest.mark.parametrize(
    "running",
    [
        pytest.param('6.8.0-151-generic"', id="quote"),
        pytest.param("6.8.0-151-generic\\", id="backslash"),
        pytest.param("6.8.0-151-generic\njess_nvidia_module_present 1", id="newline"),
        pytest.param("6.8.0-151-generic}", id="brace"),
        pytest.param("6.8.0 151", id="space"),
        pytest.param("../6.8.0-151-generic", id="slash"),
        pytest.param("", id="empty"),
    ],
)
def test_exit_2_on_unsafe_label_characters(host, capsys, running):
    host.kernel(K151)
    assert cnm.main(host.args(running)) == 2
    assert "unexpected characters" in capsys.readouterr().err
    assert list(host.metrics.iterdir()) == []


def test_default_running_kernel_is_uname_release(host, capsys):
    release = os.uname().release
    host.kernel(K151)
    argv = ["--boot-dir", str(host.boot), "--modules-dir", str(host.modules)]
    rc = cnm.main(argv)
    out = capsys.readouterr().out
    assert f"running {release}: NVIDIA module" in out
    assert rc in (0, 1)


# --------------------------------------------------------------------------
# Metrics file
# --------------------------------------------------------------------------


def test_metrics_file_parses_as_prometheus_exposition(host):
    parser = pytest.importorskip("prometheus_client.parser")
    host.kernel(K149)
    host.kernel(K151, module=None)
    cnm.main(host.args(K149))
    families = {f.name: f for f in parser.text_string_to_metric_families(host.prom())}
    present = {(s.labels["kernel"], s.labels["boot"]): s.value for s in families["jess_nvidia_module_present"].samples}
    assert present == {(K149, "running"): 1.0, (K151, "next"): 0.0}
    (stamp,) = families["jess_nvidia_module_check_timestamp_seconds"].samples
    assert stamp.value == 1_800_000_000
    assert "jess_nvidia_module_check_timestamp_seconds 1800000000\n" in host.prom()  # whole seconds, no exponent
    assert {f.type for f in families.values()} == {"gauge"}


def test_metrics_write_replaces_atomically_and_is_world_readable(host):
    host.kernel(K151)
    (host.metrics / PROM).write_text('jess_nvidia_module_present{kernel="stale",boot="next"} 0\n')
    (host.metrics / PROM).chmod(0o600)
    (host.metrics / "other_collector.prom").write_text("other 1\n")
    old_umask = os.umask(0o077)  # a strict umask must not leave node_exporter unable to read it
    try:
        assert cnm.main(host.args(K151)) == 0
    finally:
        os.umask(old_umask)
    assert "stale" not in host.prom()
    assert _rows(host.prom()) == {(K151, "running"): 1, (K151, "next"): 1}
    assert stat.S_IMODE((host.metrics / PROM).stat().st_mode) == 0o644
    # no temp file left behind, the other collector's file untouched
    assert sorted(p.name for p in host.metrics.iterdir()) == sorted(["other_collector.prom", PROM])


def test_failed_replace_cleans_up_its_temp_file(host):
    host.kernel(K151)
    (host.metrics / PROM).mkdir()  # os.replace(file, directory) fails
    assert cnm.main(host.args(K151)) == 2
    assert sorted(p.name for p in host.metrics.iterdir()) == [PROM]
    assert (host.metrics / PROM).is_dir()


def test_without_metrics_dir_nothing_is_written(host, capsys, monkeypatch):
    host.kernel(K149)
    host.kernel(K151, module=None)
    monkeypatch.chdir(host.root)
    before = host.tree()
    assert cnm.main(host.args(K149, False)) == 1
    assert host.tree() == before
    out = capsys.readouterr().out
    assert f"running {K149}: NVIDIA module present" in out
    assert f"next    {K151}: NVIDIA module MISSING" in out


def test_check_never_changes_boot_or_modules(host):
    host.kernel(K149)
    host.kernel(K151, "updates/dkms/nvidia.ko.xz")
    before = {p: p.stat().st_mtime_ns for p in host.root.rglob("*") if host.metrics not in (p, *p.parents)}
    assert cnm.main(host.args(K149)) == 0
    after = {p: p.stat().st_mtime_ns for p in host.root.rglob("*") if host.metrics not in (p, *p.parents)}
    assert after == before
