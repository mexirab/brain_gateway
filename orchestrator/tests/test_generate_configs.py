"""Tests for scripts/generate-configs.sh: the self-investigator webhook block,
the llama-primary scrape auth, render failures, and the off-box dead-man's
switch blocks (see the section near the end).

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
DEADMAN_TOKEN = "deadman-" + "d" * 24


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
    ("extra", "warns_short"),
    [
        pytest.param({}, False, id="no-token-no-flag"),
        pytest.param({"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN}, False, id="token-investigate-default-off"),
        pytest.param(
            {"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "false"}, False, id="investigate-off"
        ),
        # config.py drops a token this short, so the receiver would post into a 401
        pytest.param({"SELF_AUDIT_WEBHOOK_TOKEN": "s" * 23, "SELF_AUDIT_INVESTIGATE": "true"}, True, id="token-23"),
        pytest.param({"SELF_AUDIT_INVESTIGATE": "true"}, False, id="flag-without-token"),
    ],
)
def test_webhook_stripped_unless_both_conditions(project, extra, warns_short):
    r = _run(project, {**_PUSHOVER, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("shorter than 24 chars" in r.stdout) is warns_short
    rendered, token_file = _am(project)
    assert "self-investigator" not in rendered
    assert "/api/self_audit/alert" not in rendered
    assert "self_audit_webhook_token" not in rendered
    assert not token_file.exists()
    # The rest of the routing tree survives the sed range delete.
    assert 'name: "pushover"' in rendered and 'name: "pushover-quiet"' in rendered
    assert "user_key: ukey-abc" in rendered


def test_no_tmp_template_left_behind(project, tmp_path):
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    r = _run(project, _PUSHOVER, extra_env={"TMPDIR": str(tmpdir)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert list(tmpdir.iterdir()) == []


def test_missing_pushover_skips_alertmanager_entirely(project):
    r = _run(project, {"SELF_AUDIT_WEBHOOK_TOKEN": TOKEN, "SELF_AUDIT_INVESTIGATE": "true"})
    assert r.returncode == 0
    root, _ = project
    assert not (root / "monitoring" / "alertmanager" / "alertmanager.yml").exists()
    assert not (root / "monitoring" / "alertmanager" / "self_audit_webhook_token").exists()


def test_renders_and_token_files_keep_their_inodes(project):
    """Bind-mount inode rule: every file the script rewrites (the alertmanager render, the webhook,
    llama and deadman token files) keeps its inode across runs — rotated, or cleared when its
    feature is switched off. Also: "on" / "yes" count as truthy for SELF_AUDIT_INVESTIGATE."""
    root, _ = project
    out = root / "monitoring" / "alertmanager" / "alertmanager.yml"
    sa_token = root / "monitoring" / "alertmanager" / "self_audit_webhook_token"
    deadman = root / "monitoring" / "alertmanager" / "deadman_webhook_token"
    llama = root / "monitoring" / "prometheus" / "llama_api_key"
    first = {
        **_PUSHOVER,
        **_BRAIN_ENV,
        "MODEL_API_KEY": LLAMA_KEY,
        "SELF_AUDIT_WEBHOOK_TOKEN": TOKEN,
        "SELF_AUDIT_INVESTIGATE": "on",
        "DEADMAN_WEBHOOK_TOKEN": DEADMAN_TOKEN,
    }
    assert _run(project, first).returncode == 0
    files = (out, sa_token, deadman, llama)
    inodes = [f.stat().st_ino for f in files]
    second = {
        **_PUSHOVER,
        **_BRAIN_ENV,
        "MODEL_API_KEY": "rotated-" + "r" * 20,
        "SELF_AUDIT_WEBHOOK_TOKEN": "u" * 30,
        "SELF_AUDIT_INVESTIGATE": "yes",
    }  # deadman switched off
    r = _run(project, second)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [f.stat().st_ino for f in files] == inodes
    assert sa_token.read_text() == "u" * 30
    assert llama.read_text() == "rotated-" + "r" * 20
    assert deadman.read_text() == ""
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
    print MODEL_API_KEY or the webhook tokens."""
    r = _run(
        project,
        {
            **_PUSHOVER,
            **_BRAIN_ENV,
            "MODEL_API_KEY": LLAMA_KEY,
            "SELF_AUDIT_WEBHOOK_TOKEN": TOKEN,
            "SELF_AUDIT_INVESTIGATE": "true",
            "DEADMAN_WEBHOOK_TOKEN": DEADMAN_TOKEN,
        },
        bash_args=("-x",),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert LLAMA_KEY not in out
    assert TOKEN not in out
    assert DEADMAN_TOKEN not in out
    root, _ = project
    assert (root / "monitoring" / "alertmanager" / "deadman_webhook_token").read_text() == DEADMAN_TOKEN
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


# -- Off-box dead-man's switch (monitoring/deadman/) --------------------------
# The Watchdog route + "deadman" receiver (alertmanager) and the
# jupiter-deadman scrape job (prometheus) render only with a
# DEADMAN_WEBHOOK_TOKEN Saturn accepts (>= 24 chars). Otherwise Alertmanager
# would post heartbeats that 401 every minute and Prometheus would scrape a
# switch that isn't installed (DeadmanSwitchBroken).


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
    prom = _prom(project)[0]
    assert "job_name: 'jupiter-deadman'" in prom and "targets: ['10.0.0.58:9466']" in prom
    # Watchdog must be routed before anything that could page on it.
    assert rendered.index('alertname="Watchdog"') < rendered.index('receiver: "pushover"')


def test_deadman_host_and_port_override(project):
    r = _run(
        project, {**_PUSHOVER, "DEADMAN_WEBHOOK_TOKEN": TOKEN, "DEADMAN_HOST": "192.0.2.7", "DEADMAN_PORT": "9999"}
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'url: "http://192.0.2.7:9999/heartbeat"' in _am(project)[0]
    assert "targets: ['192.0.2.7:9999']" in _prom(project)[0]


@pytest.mark.parametrize("extra", [{}, {"DEADMAN_WEBHOOK_TOKEN": "d" * 23}])
def test_deadman_stripped_without_usable_token(project, extra):
    r = _run(project, {**_PUSHOVER, **extra})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _am(project)
    assert 'receiver: "deadman"' not in rendered and "/heartbeat" not in rendered
    assert 'alertname="Watchdog"' not in rendered  # falls through to "null"
    assert not _deadman_token_file(project).exists()
    assert "jupiter-deadman" not in _prom(project)[0]
    # The rest of both configs survives the range delete.
    assert 'name: "pushover"' in rendered and 'name: "pushover-quiet"' in rendered
    assert "job_name: 'orchestrator'" in _prom(project)[0]
    assert "job_name: 'netdata'" in _prom(project)[0]  # a job AFTER the block survives too
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
    assert "jupiter-deadman" in _prom(project)[0]
    r = _run(project, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "jupiter-deadman" not in _prom(project)[0]


@pytest.mark.parametrize("sub", ["prometheus", "alertmanager"])
def test_begin_end_markers_pair_up_in_templates(sub):
    """Guard for the real templates: every block strip_block may remove is closed."""
    import re

    text = (_MON / sub / f"{sub}.yml.template").read_text()
    for kind in ("deadman", "self-investigator", "llama-auth"):
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
    r = _run(project, _PUSHOVER, extra_env={"TMPDIR": str(tmpdir)})
    assert r.returncode != 0
    assert "'# BEGIN self-investigator' and 1 '# END self-investigator' markers — they must pair up" in r.stdout
    assert out.read_text() == before and out.stat().st_ino == inode
    assert list(tmpdir.iterdir()) == []


# -- Jupiter voice card (JUPITER_TTS_GPU_UUID) -----------------------------------
# Jupiter's two RTX 5080s are identical, so TTSGPUMemoryLow finds the voice card by
# a gpu_role="tts" label that a metric relabel adds by UUID. The exporter's uuid
# label has no "GPU-" prefix; the value lands in a regex, so a malformed one must
# fail the render rather than match the wrong card (or every card).

FAKE_UUID = "GPU-12345678-abcd-ef01-2345-6789abcdef01"  # not a real card


def _tts_relabels(rendered: str) -> list[dict]:
    job = _prom_jobs(rendered)["gpu-exporter"]
    return [r for r in job.get("metric_relabel_configs", []) if r.get("target_label") == "gpu_role"]


def test_tts_gpu_uuid_renders_the_relabel(project):
    r = _run(project, {"JUPITER_TTS_GPU_UUID": FAKE_UUID})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _prom(project)
    (relabel,) = _tts_relabels(rendered)
    assert relabel["source_labels"] == ["node", "uuid"]
    assert relabel["regex"] == "jupiter;12345678-abcd-ef01-2345-6789abcdef01"
    assert relabel["replacement"] == "tts"
    targets = {sc["labels"]["node"]: sc["targets"] for sc in _prom_jobs(rendered)["gpu-exporter"]["static_configs"]}
    assert targets["jupiter"] == ["10.0.0.248:9400"]
    assert "JUPITER_TTS_GPU_UUID is not set" not in r.stdout


def test_tts_gpu_uuid_unset_renders_a_value_no_card_has(project):
    r = _run(project, {})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "JUPITER_TTS_GPU_UUID is not set" in r.stdout
    rendered, _ = _prom(project)
    (relabel,) = _tts_relabels(rendered)
    # Never empty: an empty regex would match series with no uuid label at all.
    assert relabel["regex"] == "jupiter;unset"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("12345678-abcd-ef01-2345-6789abcdef01", id="no-GPU-prefix"),
        pytest.param("GPU-12345678-ABCD-EF01-2345-6789ABCDEF01", id="uppercase"),
        pytest.param("GPU-1234", id="short"),
        pytest.param("GPU-12345678-abcd-ef01-2345-6789abcdef01|.*", id="regex-injection"),
        pytest.param("GPU-12345678-abcd-ef01-2345-6789abcdef01 ", id="trailing-space"),
    ],
)
def test_tts_gpu_uuid_malformed_fails_and_keeps_the_live_render(project, value):
    root, _ = project
    out = root / "monitoring" / "prometheus" / "prometheus.yml"
    out.write_text("previous render\n")
    r = _run(project, {"JUPITER_TTS_GPU_UUID": f"'{value}'"})
    assert r.returncode != 0
    assert "JUPITER_TTS_GPU_UUID must look like GPU-" in r.stdout
    assert out.read_text() == "previous render\n"


def test_tts_probe_uses_the_host_gateway_name(project):
    r = _run(project, {})
    assert r.returncode == 0, r.stdout + r.stderr
    rendered, _ = _prom(project)
    bb = _prom_jobs(rendered)["blackbox-http"]
    tts = [sc for sc in bb["static_configs"] if sc["labels"].get("service") == "tts-voxcpm"]
    assert len(tts) == 1
    assert tts[0]["targets"] == ["http://host.docker.internal:8004/health"]
    assert tts[0]["labels"]["node"] == "jupiter"
