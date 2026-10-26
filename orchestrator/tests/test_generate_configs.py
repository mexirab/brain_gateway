"""Tests for scripts/generate-configs.sh: the self-investigator webhook block
and the off-box dead-man's switch blocks (see the section near the end).

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


def _run(project, env_lines: dict[str, str], tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    root, bindir = project
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_lines.items()))
    env = {"PATH": str(bindir), "HOME": str(root)}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        [str(bindir / "bash"), str(root / "scripts" / "generate-configs.sh")],
        env=env,
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


# -- Off-box dead-man's switch (monitoring/deadman/) --------------------------
# The Watchdog route + "deadman" receiver (alertmanager) and the
# jupiter-deadman scrape job (prometheus) render only with a
# DEADMAN_WEBHOOK_TOKEN Saturn accepts (>= 24 chars). Otherwise Alertmanager
# would post heartbeats that 401 every minute and Prometheus would scrape a
# switch that isn't installed (DeadmanSwitchBroken).


def _prom(project) -> str:
    root, _ = project
    return (root / "monitoring" / "prometheus" / "prometheus.yml").read_text()


def _deadman_token_file(project) -> Path:
    root, _ = project
    return root / "monitoring" / "alertmanager" / "deadman_webhook_token"


def test_deadman_token_renders_route_receiver_and_scrape(project):
    r = _run(project, {**_PUSHOVER, "DEADMAN_WEBHOOK_TOKEN": TOKEN})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _am(project)
    assert 'alertname="Watchdog"' in rendered and 'receiver: "deadman"' in rendered
    assert 'url: "http://10.0.0.58:9466/heartbeat"' in rendered  # NODE_SATURN_IP default
    assert "credentials_file: /etc/alertmanager/deadman_webhook_token" in rendered
    assert TOKEN not in rendered
    token_file = _deadman_token_file(project)
    assert token_file.read_text() == TOKEN
    assert oct(token_file.stat().st_mode & 0o777) == "0o644"
    prom = _prom(project)
    assert "job_name: 'jupiter-deadman'" in prom and "targets: ['10.0.0.58:9466']" in prom
    # Watchdog must be routed before anything that could page on it.
    assert rendered.index('alertname="Watchdog"') < rendered.index('receiver: "pushover"')


def test_deadman_host_and_port_override(project):
    r = _run(
        project, {**_PUSHOVER, "DEADMAN_WEBHOOK_TOKEN": TOKEN, "DEADMAN_HOST": "192.0.2.7", "DEADMAN_PORT": "9999"}
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'url: "http://192.0.2.7:9999/heartbeat"' in _am(project)[0]
    assert "targets: ['192.0.2.7:9999']" in _prom(project)


@pytest.mark.parametrize("extra", [{}, {"DEADMAN_WEBHOOK_TOKEN": "d" * 23}])
def test_deadman_stripped_without_usable_token(project, extra):
    r = _run(project, {**_PUSHOVER, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _am(project)
    assert 'receiver: "deadman"' not in rendered and "/heartbeat" not in rendered
    assert 'alertname="Watchdog"' not in rendered  # falls through to "null"
    assert not _deadman_token_file(project).exists()
    assert "jupiter-deadman" not in _prom(project)
    # The rest of both configs survives the range delete.
    assert 'name: "pushover"' in rendered and 'name: "pushover-quiet"' in rendered
    assert "job_name: 'orchestrator'" in _prom(project)
    assert "job_name: 'netdata'" in _prom(project)  # a job AFTER the block survives too
    if extra:
        assert "DEADMAN_WEBHOOK_TOKEN is shorter than 24 chars" in r.stdout


def test_deadman_and_self_investigator_together(project):
    r = _run(
        project,
        {
            **_PUSHOVER,
            "DEADMAN_WEBHOOK_TOKEN": TOKEN,
            "SELF_AUDIT_WEBHOOK_TOKEN": "s" * 30,
            "SELF_AUDIT_INVESTIGATE": "true",
        },
    )
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, sa_token = _am(project)
    assert 'receiver: "deadman"' in rendered and 'receiver: "self-investigator"' in rendered
    assert sa_token.read_text() == "s" * 30 and _deadman_token_file(project).read_text() == TOKEN


def test_deadman_scrape_follows_token_even_without_pushover(project):
    """No Pushover keys skips the alertmanager render, but prometheus still renders."""
    r = _run(project, {"DEADMAN_WEBHOOK_TOKEN": TOKEN})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "jupiter-deadman" in _prom(project)
    r = _run(project, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "jupiter-deadman" not in _prom(project)


def test_deadman_token_file_cleared_when_switched_off(project):
    _run(project, {**_PUSHOVER, "DEADMAN_WEBHOOK_TOKEN": TOKEN})
    token_file = _deadman_token_file(project)
    inode = token_file.stat().st_ino
    r = _run(project, _PUSHOVER)
    assert r.returncode == 0, r.stdout + r.stderr
    assert token_file.read_text() == "" and token_file.stat().st_ino == inode


@pytest.mark.parametrize("sub", ["prometheus", "alertmanager"])
def test_begin_end_markers_pair_up_in_templates(sub):
    """Guard for the real templates: every block strip_block may remove is closed."""
    import re

    text = (_MON / sub / f"{sub}.yml.template").read_text()
    for kind in ("deadman", "self-investigator"):
        begins = len(re.findall(rf"^\s*# BEGIN {kind}( |$)", text, re.M))
        ends = len(re.findall(rf"^\s*# END {kind}( |$)", text, re.M))
        assert begins == ends, (sub, kind, begins, ends)


def test_unpaired_marker_fails_the_render(project):
    root, _ = project
    tmpl = root / "monitoring" / "prometheus" / "prometheus.yml.template"
    tmpl.write_text(tmpl.read_text().replace("# END deadman", "# (end marker lost)"))
    r = _run(project, _PUSHOVER)
    assert r.returncode != 0
    assert "must pair up" in r.stdout
    assert not (root / "monitoring" / "prometheus" / "prometheus.yml").exists()


def test_failed_strip_keeps_the_live_render_and_cleans_temp_files(project, tmp_path):
    """A lost marker in the alertmanager template fails the run after the
    deadman blocks were already stripped into temp files (prometheus and
    alertmanager): the live alertmanager.yml keeps its bytes and inode, and
    the EXIT trap removes every temp template on the failure path too."""
    root, _ = project
    assert _run(project, _PUSHOVER).returncode == 0
    out = root / "monitoring" / "alertmanager" / "alertmanager.yml"
    before, inode = out.read_text(), out.stat().st_ino
    tmpl = root / "monitoring" / "alertmanager" / "alertmanager.yml.template"
    tmpl.write_text(tmpl.read_text().replace("# END self-investigator", "# (end marker lost)", 1))
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    r = _run(project, _PUSHOVER, tmpdir=tmpdir)
    assert r.returncode != 0
    assert "'# BEGIN self-investigator' and 1 '# END self-investigator' markers — they must pair up" in r.stdout
    assert out.read_text() == before and out.stat().st_ino == inode
    assert list(tmpdir.iterdir()) == []
