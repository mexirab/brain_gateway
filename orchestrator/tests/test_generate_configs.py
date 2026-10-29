"""Tests for scripts/generate-configs.sh: the self-investigator webhook block,
the llama-primary scrape auth, and render failures.

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


def _run(
    project, env_lines: dict[str, str], *, extra_env: dict[str, str] | None = None, bash_args: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    root, bindir = project
    (root / ".env").write_text("".join(f"{k}={v}\n" for k, v in env_lines.items()))
    return subprocess.run(
        [str(bindir / "bash"), *bash_args, str(root / "scripts" / "generate-configs.sh")],
        env={"PATH": str(bindir), "HOME": str(root), **(extra_env or {})},
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


# ===========================================================================
# llama-primary scrape auth (MODEL_API_KEY -> monitoring/prometheus/llama_api_key)
# ===========================================================================

LLAMA_KEY = "llama-key-" + "k" * 22
_BRAIN_ENV = {"NODE_HELIOS_IP": "10.0.0.195", "MODEL_URL": "http://${NODE_HELIOS_IP}:8080/v1"}


def _prom(project) -> tuple[str, Path]:
    root, _ = project
    prom_dir = root / "monitoring" / "prometheus"
    return (prom_dir / "prometheus.yml").read_text(), prom_dir / "llama_api_key"


def _prom_jobs(rendered: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return {j["job_name"]: j for j in yaml.safe_load(rendered)["scrape_configs"]}


@pytest.mark.parametrize(
    "extra",
    [
        {},  # MODEL_URL=http://${NODE_HELIOS_IP}:8080/v1, backend default
        {"MODEL_URL": "http://10.0.0.195:8080/v1/"},
        {"MODEL_URL": "http://10.0.0.195:8080"},
        {"MODEL_URL": "http://10.0.0.195:8080/"},
        {"MODEL_BACKEND": "openai_compatible"},
        {"MODEL_BACKEND": "OpenAI_Compatible"},
    ],
)
def test_llama_key_renders_scrape_auth(project, extra):
    r = _run(project, {**_BRAIN_ENV, "MODEL_API_KEY": LLAMA_KEY, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, key_file = _prom(project)
    job = _prom_jobs(rendered)["llama-primary"]
    assert job["authorization"] == {"type": "Bearer", "credentials_file": "/etc/prometheus/llama_api_key"}
    assert job["follow_redirects"] is False
    assert job["static_configs"][0]["targets"] == ["10.0.0.195:8080"]
    # The key lives only in the credentials file, never in the config or output.
    assert LLAMA_KEY not in rendered
    assert LLAMA_KEY not in r.stdout + r.stderr
    assert key_file.read_bytes() == LLAMA_KEY.encode()  # exact, no trailing newline
    assert oct(key_file.stat().st_mode & 0o777) == "0o644"
    # The orchestrator job's own bearer auth is unaffected.
    assert _prom_jobs(rendered)["orchestrator"]["authorization"]["credentials_file"] == "/etc/prometheus/api_token"


def test_llama_key_file_truncated_in_place(project):
    """Directory mount, but same rule as the other token files: keep the inode."""
    _run(project, {**_BRAIN_ENV, "MODEL_API_KEY": LLAMA_KEY})
    _, key_file = _prom(project)
    inode = key_file.stat().st_ino
    r = _run(project, {**_BRAIN_ENV, "MODEL_API_KEY": "rotated-" + "r" * 20})
    assert r.returncode == 0, r.stdout + r.stderr
    assert key_file.stat().st_ino == inode
    assert key_file.read_text() == "rotated-" + "r" * 20


@pytest.mark.parametrize("extra", [{}, {"MODEL_API_KEY": ""}])
def test_no_key_strips_block_and_removes_stale_file(project, extra):
    root, _ = project
    stale = root / "monitoring" / "prometheus" / "llama_api_key"
    stale.write_text("old-stale-key")
    r = _run(project, {**_BRAIN_ENV, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, key_file = _prom(project)
    assert not key_file.exists()
    job = _prom_jobs(rendered)["llama-primary"]
    assert "authorization" not in job
    assert job["follow_redirects"] is False  # outside the stripped block
    assert job["static_configs"][0]["targets"] == ["10.0.0.195:8080"]
    assert "llama_api_key" not in rendered and "llama-auth" not in rendered
    # The sed range delete leaves the rest of the config intact.
    assert _prom_jobs(rendered)["orchestrator"]["authorization"]["credentials_file"] == "/etc/prometheus/api_token"
    assert "llama-primary scrape auth" in r.stdout  # SKIP line


@pytest.mark.parametrize(
    "extra",
    [
        {"MODEL_BACKEND": "anthropic", "MODEL_URL": "https://api.anthropic.com/v1"},
        {"MODEL_BACKEND": "anthropic", "MODEL_URL": "http://10.0.0.195:8080/v1"},  # cloud key, brain URL
        {"MODEL_BACKEND": "openai", "MODEL_URL": "http://10.0.0.195:8080/v1"},
        {"MODEL_URL": "https://api.anthropic.com/v1"},
        {"MODEL_URL": "http://10.0.0.195:8081/v1"},  # Helios, other port
        {"MODEL_URL": "http://10.0.0.195:80801/v1"},  # port-prefix lookalike
        {"MODEL_URL": "http://10.0.0.1950:8080/v1"},  # host-prefix lookalike
        {"MODEL_URL": "http://10.0.0.195.evil.com:8080/v1"},
        {"MODEL_URL": "http://10.0.0.195:8080@evil.com/v1"},  # userinfo trick
        {"MODEL_URL": "https://10.0.0.195:8080/v1"},  # other scheme
        {"MODEL_URL": "http://10.0.0.58:8080/v1"},  # other host
        {"MODEL_URL": ""},
        {"NODE_HELIOS_IP": "10.0.0.200", "MODEL_URL": "http://10.0.0.195:8080/v1"},  # job scrapes .200
    ],
)
def test_key_not_written_when_model_is_not_the_scraped_brain(project, extra):
    root, _ = project
    stale = root / "monitoring" / "prometheus" / "llama_api_key"
    stale.write_text("old-stale-key")
    r = _run(project, {**_BRAIN_ENV, "MODEL_API_KEY": LLAMA_KEY, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, key_file = _prom(project)
    assert not key_file.exists()
    assert "authorization" not in _prom_jobs(rendered)["llama-primary"]
    assert "llama_api_key" not in rendered
    assert LLAMA_KEY not in rendered
    assert LLAMA_KEY not in r.stdout + r.stderr


@pytest.mark.parametrize("keyed", [True, False])
def test_blackbox_probes_llama_health_not_models(project, keyed):
    r = _run(project, {**_BRAIN_ENV, **({"MODEL_API_KEY": LLAMA_KEY} if keyed else {})})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _prom(project)
    bb = _prom_jobs(rendered)["blackbox-http"]
    llama = [sc for sc in bb["static_configs"] if sc["labels"].get("service") == "llama-primary"]
    assert len(llama) == 1
    assert llama[0]["targets"] == ["http://10.0.0.195:8080/health"]
    # /v1/models 401s once the brain has a key; nothing may probe it unauthenticated.
    assert "8080/v1/models" not in rendered


def test_xtrace_never_echoes_the_keys(project):
    """`bash -x generate-configs.sh` (debugging in the public CI log) must not
    print MODEL_API_KEY or the webhook token."""
    r = _run(
        project,
        {
            **_PUSHOVER,
            **_BRAIN_ENV,
            "MODEL_API_KEY": LLAMA_KEY,
            "SELF_AUDIT_WEBHOOK_TOKEN": TOKEN,
            "SELF_AUDIT_INVESTIGATE": "true",
        },
        bash_args=("-x",),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert LLAMA_KEY not in out
    assert TOKEN not in out
    # Files still written, and xtrace was switched back on after each write.
    _, key_file = _prom(project)
    assert key_file.read_text() == LLAMA_KEY
    assert "chmod 644" in r.stderr and "llama_api_key" in r.stderr


# ===========================================================================
# A failing render must not blank the live (bind-mounted) config
# ===========================================================================


def _stub_envsubst(bindir: Path, body: str) -> None:
    stub = bindir / "envsubst"
    stub.unlink()
    stub.write_text(f"#!{shutil.which('bash')}\n{body}\n")
    stub.chmod(0o755)


@pytest.mark.parametrize("keyed", [False, True])
def test_envsubst_failure_leaves_live_prometheus_untouched(project, tmp_path, keyed):
    """`render_template ... || rc=$?` runs with set -e suspended: an unchecked
    envsubst failure used to fall through to `cat $tmp > prometheus.yml`."""
    root, bindir = project
    live = root / "monitoring" / "prometheus" / "prometheus.yml"
    live.write_text("# previous good render\n")
    inode = live.stat().st_ino
    _stub_envsubst(bindir, "echo 'partial: ['\nexit 1")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {**_PUSHOVER, **_BRAIN_ENV, **({"MODEL_API_KEY": LLAMA_KEY} if keyed else {})}
    r = _run(project, env, extra_env={"TMPDIR": str(tmpdir)})
    assert r.returncode != 0
    assert live.read_text() == "# previous good render\n"
    assert live.stat().st_ino == inode
    assert "envsubst failed" in r.stdout
    assert "Config generation complete" not in r.stdout
    assert list(tmpdir.iterdir()) == []  # temp render + stripped temp template cleaned up
    # The script stopped at prometheus: alertmanager was never rendered.
    assert not (root / "monitoring" / "alertmanager" / "alertmanager.yml").exists()


def test_envsubst_failure_leaves_live_alertmanager_untouched(project, tmp_path):
    root, bindir = project
    live = root / "monitoring" / "alertmanager" / "alertmanager.yml"
    live.write_text("# previous good alertmanager render\n")
    real = shutil.which("envsubst")
    # Fail only on the alertmanager template; prometheus renders normally.
    _stub_envsubst(
        bindir,
        'input="$(cat)"\n'
        'case "$input" in *pushover_configs*) echo partial; exit 1;; esac\n'
        f'printf "%s\\n" "$input" | {real} "$@"',
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    r = _run(project, {**_PUSHOVER}, extra_env={"TMPDIR": str(tmpdir)})
    assert r.returncode != 0
    assert live.read_text() == "# previous good alertmanager render\n"
    assert "envsubst failed for alertmanager.yml.template" in r.stdout
    assert "Config generation complete" not in r.stdout
    assert list(tmpdir.iterdir()) == []
    rendered, _ = _prom(project)
    assert "job_name: 'llama-primary'" in rendered  # the earlier render went through
