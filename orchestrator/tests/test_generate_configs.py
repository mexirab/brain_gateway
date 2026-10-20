"""Tests for scripts/generate-configs.sh: the self-investigator webhook block.

The alertmanager render keeps the BEGIN/END self-investigator blocks (and
writes the token file the receiver reads) only when SELF_AUDIT_WEBHOOK_TOKEN
is >= 24 chars AND SELF_AUDIT_INVESTIGATE is truthy — the same conditions
under which the orchestrator accepts the webhook. Otherwise Alertmanager
would post without a token, or into a 409/401 it re-sends forever.

Runs the real script against a temp copy of the repo layout with a PATH that
has no `docker`, so the amtool/promtool validation is skipped and nothing
touches the live monitoring dir. Needs bash + envsubst (gettext): the
orchestrator image has no envsubst, so there this file skips — run it on the
host with `python3 -m pytest --noconftest orchestrator/tests/test_generate_configs.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "generate-configs.sh"
_MON = _REPO / "monitoring"

_TOOLS = (
    "bash",
    "envsubst",
    "dirname",
    "basename",
    "mktemp",
    "cat",
    "chmod",
    "rm",
    "mkdir",
    "sed",
    "id",
    "grep",
    "head",
)

pytestmark = pytest.mark.skipif(
    not (_SCRIPT.is_file() and (_MON / "alertmanager" / "alertmanager.yml.template").is_file())
    or any(shutil.which(t) is None for t in _TOOLS),
    reason="needs scripts/ + monitoring/ and bash/envsubst/coreutils",
)

TOKEN = "t" * 32


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_SCRIPT, root / "scripts" / "generate-configs.sh")
    for sub in ("prometheus", "alertmanager"):
        (root / "monitoring" / sub).mkdir(parents=True)
        shutil.copy2(_MON / sub / f"{sub}.yml.template", root / "monitoring" / sub / f"{sub}.yml.template")
    # PATH with only the tools the script needs — crucially no docker, so the
    # validators are skipped (WARN) instead of starting containers.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for t in _TOOLS:
        (bindir / t).symlink_to(shutil.which(t))
    return root, bindir


def _run(project, env_lines: dict[str, str]) -> subprocess.CompletedProcess:
    root, bindir = project
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_lines.items()))
    return subprocess.run(
        [str(bindir / "bash"), str(root / "scripts" / "generate-configs.sh")],
        env={"PATH": str(bindir), "HOME": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _am(project) -> tuple[str, Path]:
    root, _ = project
    am_dir = root / "monitoring" / "alertmanager"
    return (am_dir / "alertmanager.yml").read_text(), am_dir / "self_audit_webhook_token"


_PUSHOVER = {"PUSHOVER_USER_KEY": "ukey-abc", "PUSHOVER_APP_TOKEN": "atok-xyz"}


def test_token_and_investigate_keep_the_webhook(project):
    r = _run(project, {**_PUSHOVER, "SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "True"})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, token_file = _am(project)
    assert 'receiver: "self-investigator"' in rendered
    assert "http://10.0.0.248:8888/api/self_audit/alert" in rendered  # defaults substituted
    assert "${" not in rendered.split("pushover_configs")[0]  # nothing left unsubstituted
    assert token_file.read_text() == TOKEN  # no trailing newline in a credentials_file
    assert oct(token_file.stat().st_mode & 0o777) == "0o644"
    # The token itself never lands in the rendered config, only the file path.
    assert TOKEN not in rendered
    assert "user_key: ukey-abc" in rendered


@pytest.mark.parametrize(
    "extra",
    [
        {},  # no token, no flag
        {"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN},  # token but investigate off (default)
        {"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "false"},
        {"SELF_AUDIT_WEBHOOK_TOKEN": "s" * 23, "SELF_AUDIT_INVESTIGATE": "true"},  # config.py drops it
        {"SELF_AUDIT_INVESTIGATE": "true"},  # flag without token
    ],
)
def test_webhook_stripped_unless_both_conditions(project, extra):
    r = _run(project, {**_PUSHOVER, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, token_file = _am(project)
    assert "self-investigator" not in rendered
    assert "/api/self_audit/alert" not in rendered
    assert "self_audit_webhook_token" not in rendered
    assert not token_file.exists()
    # The rest of the routing tree survives the sed range delete.
    assert 'name: "pushover"' in rendered and 'name: "pushover-quiet"' in rendered
    assert "user_key: ukey-abc" in rendered


def test_short_token_warns(project):
    r = _run(project, {**_PUSHOVER, "SELF_AUDIT_WEBHOOK_TOKEN": "short", "SELF_AUDIT_INVESTIGATE": "true"})
    assert r.returncode == 0
    assert "shorter than 24 chars" in r.stdout


def test_no_tmp_template_left_behind(project, tmp_path):
    root, bindir = project
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in _PUSHOVER.items()))
    r = subprocess.run(
        [str(bindir / "bash"), str(root / "scripts" / "generate-configs.sh")],
        env={"PATH": str(bindir), "HOME": str(root), "TMPDIR": str(tmpdir)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert list(tmpdir.iterdir()) == []


def test_missing_pushover_skips_alertmanager_entirely(project):
    r = _run(project, {"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "true"})
    assert r.returncode == 0
    root, _ = project
    assert not (root / "monitoring" / "alertmanager" / "alertmanager.yml").exists()
    assert not (root / "monitoring" / "alertmanager" / "self_audit_webhook_token").exists()


def test_existing_render_is_truncated_in_place(project):
    """Bind-mount inode rule: the render must keep its inode across runs."""
    _run(project, {**_PUSHOVER, "SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "on"})
    root, _ = project
    out = root / "monitoring" / "alertmanager" / "alertmanager.yml"
    tok = root / "monitoring" / "alertmanager" / "self_audit_webhook_token"
    inode, tok_inode = out.stat().st_ino, tok.stat().st_ino
    r = _run(project, {**_PUSHOVER, "SELF_AUDIT_WEBHOOK_TOKEN": "u" * 30, "SELF_AUDIT_INVESTIGATE": "yes"})
    assert r.returncode == 0
    assert out.stat().st_ino == inode and tok.stat().st_ino == tok_inode
    assert tok.read_text() == "u" * 30
    assert os.access(out, os.R_OK)
