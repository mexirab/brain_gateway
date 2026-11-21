#!/usr/bin/env python3
"""Warn before a reboot loses the NVIDIA driver: is there a module for the next kernel?

Why: on Ubuntu the NVIDIA kernel module ships as prebuilt packages per kernel
(linux-modules-nvidia-580-open-<kernel>). unattended-upgrades installs new
security kernels, but keeps the NVIDIA packages back whenever upgrading them
means removing an old module package, which it never does. The next reboot then
boots a kernel with no NVIDIA module: nvidia-smi fails and every GPU service is
down. That happened on Helios (2026-10-15), Saturn (2026-11-07) and Jupiter
(2026-11-21). The fix is one `sudo apt full-upgrade`, which moves the kernel and
its modules together, but only if someone knows before rebooting.

What it checks (read-only, stdlib only, no root needed):
- the running kernel, os.uname().release;
- the kernel the next boot picks: the newest /boot/vmlinuz-<version> by version
  (GRUB_DEFAULT=0 boots the newest installed kernel; a host with a pinned GRUB
  default would need this changed);
- for each, whether /lib/modules/<kernel>/ holds an nvidia.ko (any compression,
  any subdirectory, so DKMS builds count too).

Metrics (--metrics-dir, a node_exporter textfile directory), file
jess_nvidia_modules.prom, written atomically:
  jess_nvidia_module_present{kernel="<release>",boot="running"|"next"} 1|0
  jess_nvidia_module_check_timestamp_seconds <unix time>
When the running kernel is also the newest, both rows name the same kernel.
Alerts: NvidiaModuleMissing (a row is 0), NvidiaModuleCheckStale (the timestamp
stops moving). Without --metrics-dir it prints the rows and writes nothing.

Exit codes: 0 = every checked kernel has a module; 1 = a module is missing (the
systemd unit then shows failed in `systemctl --failed`, the metric drives the
alert); 2 = the check itself failed (no kernels found, unwritable metrics dir).
Never changes anything on the host.

Install (Jupiter, as labadmin; units in scripts/nvidia-module-check/):
  sudo install -m 755 scripts/check_nvidia_modules.py /usr/local/sbin/check-nvidia-modules
  sudo install -m 644 scripts/nvidia-module-check/nvidia-module-check.service \
      scripts/nvidia-module-check/nvidia-module-check.timer /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now nvidia-module-check.timer
  sudo systemctl start nvidia-module-check.service
  cat /home/labadmin/node_exporter/textfile_collector/jess_nvidia_modules.prom
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path

METRICS_FILE = "jess_nvidia_modules.prom"
MODULE_NAMES = ("nvidia.ko", "nvidia.ko.zst", "nvidia.ko.xz", "nvidia.ko.gz")
# Kernel release strings as Ubuntu names them: 6.8.0-151-generic. Anything else in
# /boot (vmlinuz, vmlinuz.old symlinks, *.efi.signed) is skipped.
_RELEASE_RE = re.compile(r"^\d+\.\d+\.\d+-\d+[-.\w]*$")
# Label values go into the exposition format; a release string never needs more.
_LABEL_SAFE_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


def _version_key(release: str) -> tuple[int, ...]:
    """Order releases numerically: 6.8.0-151-generic > 6.8.0-149-generic > 6.8.0-99-generic."""
    return tuple(int(n) for n in re.findall(r"\d+", release))


def installed_kernels(boot_dir: Path) -> list[str]:
    releases = []
    for entry in boot_dir.glob("vmlinuz-*"):
        release = entry.name[len("vmlinuz-"):]
        if _RELEASE_RE.match(release) and not entry.is_symlink():
            releases.append(release)
    return sorted(releases, key=_version_key)


def module_present(modules_dir: Path, release: str) -> bool:
    root = modules_dir / release
    if not root.is_dir():
        return False
    for _dirpath, _dirnames, filenames in os.walk(root):
        if any(name in MODULE_NAMES for name in filenames):
            return True
    return False


def check(boot_dir: Path, modules_dir: Path, running: str) -> list[tuple[str, str, bool]]:
    """Rows of (kernel, boot, present). Raises RuntimeError if no kernel is installed."""
    kernels = installed_kernels(boot_dir)
    if not kernels:
        raise RuntimeError(f"no kernel images found in {boot_dir}")
    newest = kernels[-1]
    return [
        (running, "running", module_present(modules_dir, running)),
        (newest, "next", module_present(modules_dir, newest)),
    ]


def render(rows: list[tuple[str, str, bool]], now: float) -> str:
    lines = [
        "# HELP jess_nvidia_module_present 1 if an NVIDIA kernel module is installed for this kernel.",
        "# TYPE jess_nvidia_module_present gauge",
    ]
    for kernel, boot, present in rows:
        if not _LABEL_SAFE_RE.match(kernel):
            raise RuntimeError("unexpected characters in a kernel release string")
        lines.append(f'jess_nvidia_module_present{{kernel="{kernel}",boot="{boot}"}} {int(present)}')
    lines += [
        "# HELP jess_nvidia_module_check_timestamp_seconds When check_nvidia_modules last ran.",
        "# TYPE jess_nvidia_module_check_timestamp_seconds gauge",
        f"jess_nvidia_module_check_timestamp_seconds {int(now)}",
    ]
    return "\n".join(lines) + "\n"


def write_atomic(directory: Path, name: str, text: str) -> None:
    """node_exporter must never read a half-written file: temp file in the same dir, then rename."""
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, directory / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--metrics-dir", type=Path, help="node_exporter textfile collector directory")
    ap.add_argument("--boot-dir", type=Path, default=Path("/boot"), help=argparse.SUPPRESS)
    ap.add_argument("--modules-dir", type=Path, default=Path("/lib/modules"), help=argparse.SUPPRESS)
    ap.add_argument("--running", default=os.uname().release, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        rows = check(args.boot_dir, args.modules_dir, args.running)
        text = render(rows, time.time())
        if args.metrics_dir:
            write_atomic(args.metrics_dir, METRICS_FILE, text)
    except (OSError, RuntimeError) as exc:
        print(f"check-nvidia-modules: check failed: {exc}", file=sys.stderr)
        return 2
    for kernel, boot, present in rows:
        print(f"{boot:7} {kernel}: NVIDIA module {'present' if present else 'MISSING'}")
    missing = [kernel for kernel, _boot, present in rows if not present]
    if missing:
        print(
            "check-nvidia-modules: no NVIDIA module for " + ", ".join(sorted(set(missing)))
            + ". Before rebooting run: sudo apt full-upgrade",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
