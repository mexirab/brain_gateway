"""Coder box (coder/, scripts/jess-coder): static hardening guards + launcher logic.

The box runs model-written shell commands; docs/CODER.md has the threat model.
These tests pin what makes it a box:

- the image: Jess's dependency layers (same lines as orchestrator/Dockerfile)
  but none of her code, its own uid (no host account has it), no healthcheck,
  pinned Qwen Code + Node;
- Qwen Code's locked settings: the four background features that make extra
  brain calls are off, no telemetry/updates, the token comes from the env;
- the compose service is build-only and can't do anything if started;
- every box container comes from ONE function with the hardening flags;
- the patch check judges a box's diff by what trusted git makes of it in a
  clean clone, so hostile diff text (odd mode lines, decoy hunk lines, "no
  newline" joins, forged renames) can't slip past it;
- the self-test's probes tell "no route" from "something answered".

Needs a full checkout (coder/, scripts/); skips otherwise, like
test_coder_hardening.py. The git-backed patch tests also need git.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CODER = _REPO / "coder"
_LAUNCHER = _REPO / "scripts" / "jess-coder"

pytestmark = pytest.mark.skipif(
    not (_CODER / "Dockerfile").is_file() or not _LAUNCHER.is_file(),
    reason="needs a full repo checkout (coder/, scripts/jess-coder)",
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _load(name: str, path: Path) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def jc() -> ModuleType:
    return _load("jess_coder_launcher", _LAUNCHER)


@pytest.fixture(scope="module")
def entry() -> ModuleType:
    return _load("jess_coder_entrypoint", _CODER / "entrypoint.py")


@pytest.fixture(scope="module")
def selftest() -> ModuleType:
    return _load("jess_coder_selftest", _CODER / "selftest.py")


@pytest.fixture
def cfg(jc, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("API_TOKEN=t\nCODER_ENABLED=true\n")
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(tmp_path))
    for key in ("JESS_CODER_IMAGE", "JESS_CODER_NETWORK", "JESS_CODER_GATE_CONTAINER", "JESS_CODER_HOME"):
        monkeypatch.delenv(key, raising=False)
    return jc.Config()


# ------------------------------------------------------------------- image


def _dockerfile() -> str:
    return (_CODER / "Dockerfile").read_text()


def _instructions(text: str) -> list:
    """Dockerfile instructions without comments/blank lines, continuations joined."""
    out, cur = [], ""
    for line in text.splitlines():
        if not cur and (not line.strip() or line.lstrip().startswith("#")):
            continue
        cur += line.rstrip("\\").strip() + " " if line.rstrip().endswith("\\") else line.strip()
        if not line.rstrip().endswith("\\"):
            out.append(cur.strip())
            cur = ""
    return out


def test_image_repeats_the_orchestrator_dependency_lines():
    """Same instructions = BuildKit reuses the orchestrator's pip layer and the
    box tests against the same packages."""
    orch = _instructions((_REPO / "orchestrator" / "Dockerfile").read_text())[:5]
    box = _instructions(_dockerfile())[:5]
    assert orch[0] == box[0] == "FROM python:3.11-slim"
    assert box[3] == "COPY --from=orchestrator requirements.txt ."
    assert orch[3] == "COPY requirements.txt ."
    assert [orch[i] for i in (0, 1, 2, 4)] == [box[i] for i in (0, 1, 2, 4)]


def test_image_takes_no_orchestrator_code():
    copies = [i for i in _instructions(_dockerfile()) if i.startswith("COPY --from=orchestrator")]
    assert copies == [
        "COPY --from=orchestrator requirements.txt .",
        "COPY --from=orchestrator embedding_pin.py /opt/jess-coder/pin/embedding_pin.py",
    ]
    assert "test ! -e /app/orchestrator" in _dockerfile()
    froms = re.findall(r"^FROM\s+(\S+)", _dockerfile(), re.M)
    assert froms == ["python:3.11-slim"]


def test_image_uid_matches_launcher_and_is_not_a_login(jc):
    m = re.search(r"^ARG CODER_UID=(\d+)$", _dockerfile(), re.M)
    assert m and int(m.group(1)) == jc.BOX_UID
    assert jc.BOX_UID not in (0, 1000, 1001)  # root, the owner/orchestrator, another login on Jupiter


def test_image_runtime_settings():
    text = _dockerfile()
    assert re.search(r"^HEALTHCHECK NONE$", text, re.M)
    assert re.search(r"^USER coder$", text, re.M)
    assert '"python3", "-I", "-S", "/opt/jess-coder/entrypoint.py"' in text
    assert "HF_HUB_OFFLINE=1" in text
    assert not re.search(r"^# syntax=", text, re.M), "no remote Dockerfile frontend (it controls the whole build)"


def test_image_pins_node_and_qwen_code():
    text = _dockerfile()
    assert re.search(r"^ARG NODE_SHA256=[0-9a-f]{64}$", text, re.M)
    assert "ADD --checksum=sha256:${NODE_SHA256}" in text
    assert "npm ci --ignore-scripts" in text
    pkg = json.loads((_CODER / "package.json").read_text())
    version = pkg["dependencies"]["@qwen-code/qwen-code"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), "pin an exact Qwen Code version"
    lock = json.loads((_CODER / "package-lock.json").read_text())
    entry = lock["packages"]["node_modules/@qwen-code/qwen-code"]
    assert entry["version"] == version and entry["integrity"].startswith("sha512-")


# ----------------------------------------------------------------- settings


def _settings() -> dict:
    raw = (_CODER / "settings.json").read_text()
    raw = raw.replace("@GATE_URL@", "http://10.213.250.2:8888/api/coder/v1").replace("@CONTEXT_WINDOW@", "131072")
    return json.loads(raw)


def test_settings_turn_off_background_brain_calls():
    s = _settings()
    assert s["memory"] == {"enableManagedAutoMemory": False, "enableManagedAutoDream": False, "enableAutoSkill": False}
    assert s["ui"]["enableFollowupSuggestions"] is False


def test_settings_no_telemetry_updates_hooks_or_mcp():
    s = _settings()
    assert s["privacy"]["usageStatisticsEnabled"] is False
    assert s["telemetry"]["enabled"] is False
    assert s["general"]["enableAutoUpdate"] is False
    assert s["disableAllHooks"] is True
    assert s["mcpServers"] == {}


def test_settings_token_from_env_and_gate_url_placeholder():
    raw = (_CODER / "settings.json").read_text()
    assert "@GATE_URL@" in raw and "@CONTEXT_WINDOW@" in raw
    provider = _settings()["modelProviders"]["openai"]
    assert len(provider) == 1
    assert provider[0]["envKey"] == "JESS_BRAIN_KEY"
    assert "apiKey" not in json.dumps(provider)
    gen = provider[0]["generationConfig"]
    # The gate can hold a request ~300 s before a <=240 s attempt (coder_gate MAX_HOLD_S).
    assert gen["timeout"] >= 600_000 and gen["streamIdleTimeoutMs"] >= 600_000


# ------------------------------------------------------------------ compose


def test_compose_service_is_build_only():
    compose = yaml.safe_load((_REPO / "docker-compose.yml").read_text())
    svc = compose["services"]["coder"]
    assert svc["profiles"] == ["coder"]
    assert svc["entrypoint"] == ["true"]
    assert svc["network_mode"] == "none"
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert svc["restart"] == "no"
    for key in ("ports", "volumes", "networks", "environment", "env_file", "extra_hosts", "privileged", "devices"):
        assert key not in svc, key
    # A source context: building the box never builds (or re-tags) the orchestrator's image.
    assert svc["build"]["additional_contexts"] == {"orchestrator": "./orchestrator"}
    assert "${CODER_GATE_ADDRESS:-10.213.250.2}" in svc["build"]["args"]["GATE_URL"]
    assert svc["build"]["labels"] == {"jess.coder.inputs": "${JESS_CODER_INPUTS:-unknown}"}
    assert svc["image"] == "jess-coder:current"


# ------------------------------------------------------------ box command


def _flag_value(cmd: list, flag: str) -> list:
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == flag]


def test_box_command_hardening(jc, cfg, tmp_path):
    cmd = jc.box_command(
        cfg,
        name="demo",
        kind="session",
        mode=["session"],
        network=cfg.network,
        cidfile=tmp_path / "cid",
        tty=True,
        workspace=tmp_path / "box",
        bundle=tmp_path / "b.bundle",
        env={"JESS_CODER_NAME": "demo"},
        secret_env=["JESS_BRAIN_KEY"],
    )
    joined = " ".join(cmd)
    for flag in ("--rm", "--init", "--read-only"):
        assert flag in cmd
    assert _flag_value(cmd, "--pull") == ["never"]
    assert _flag_value(cmd, "--cap-drop") == ["ALL"]
    assert _flag_value(cmd, "--security-opt") == ["no-new-privileges:true"]
    assert _flag_value(cmd, "--user") == [f"{jc.BOX_UID}:{jc.BOX_UID}"]
    assert _flag_value(cmd, "--log-driver") == ["none"]
    assert _flag_value(cmd, "--network") == ["gateway_nerves_coder-net"]
    assert _flag_value(cmd, "--name") == ["jess-coder-demo"]
    assert _flag_value(cmd, "--cidfile") == [str(tmp_path / "cid")]
    assert "jess-coder.kind=session" in _flag_value(cmd, "--label")
    for flag in ("--memory", "--memory-swap", "--cpus", "--cpu-shares", "--oom-score-adj", "--pids-limit"):
        assert _flag_value(cmd, flag), flag
    for bad in (
        "--privileged",
        "--cap-add",
        "--pid",
        "--ipc",
        "--uts",
        "--userns",
        "--device",
        "-v",
        "--volume",
        "--volumes-from",
        "--add-host",
        "-p",
        "--publish",
        "--env-file",
    ):
        assert bad not in cmd, bad
    assert "docker.sock" not in joined and "--network host" not in joined
    # The token goes by name only: its value never reaches argv.
    assert "JESS_BRAIN_KEY" in _flag_value(cmd, "-e")
    assert not any(v.startswith("JESS_BRAIN_KEY=") for v in _flag_value(cmd, "-e"))
    mounts = _flag_value(cmd, "--mount")
    assert mounts == [
        f"type=bind,src={tmp_path / 'box'},dst=/work",
        f"type=bind,src={tmp_path / 'b.bundle'},dst=/bundle/repo.bundle,readonly",
    ]
    assert cmd[-2:] == ["jess-coder:current", "session"]


def test_box_command_patch_is_offline_and_read_only(jc, cfg, tmp_path):
    cmd = jc.box_command(
        cfg,
        name="x-patch",
        kind="patch",
        mode=["patch", "a" * 40],
        network="none",
        cidfile=tmp_path / "cid",
        workspace=tmp_path,
        workspace_ro=True,
    )
    assert _flag_value(cmd, "--network") == ["none"]
    assert _flag_value(cmd, "--mount") == [f"type=bind,src={tmp_path},dst=/work,readonly"]
    assert "-it" not in cmd and "-i" not in cmd


def test_box_command_is_the_only_docker_run():
    src = _LAUNCHER.read_text()
    assert src.count('"docker", "run"') == 1
    assert "'docker', 'run'" not in src and '"docker","run"' not in src


@pytest.mark.parametrize(
    "name", ["", "-x", "A", "a b", "a/b", "a" * 33, "../x", "x;rm", "abc\n", "x-patch", "x-rm", "selftest-1"]
)
def test_bad_session_names(jc, name):
    with pytest.raises(jc.Fatal):
        jc.check_name(name)


def test_good_session_name(jc):
    assert jc.check_name("fix-grocery-1") == "fix-grocery-1"


def test_load_env(jc, tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nA=1\nexport B='two words'\nC=\"x=y\"\n\nbad line\n")
    assert jc.load_env(p) == {"A": "1", "B": "two words", "C": "x=y"}


@pytest.mark.parametrize("value, on", [("true", True), ("True", True), ('"yes"', True), ("1", True), ("false", False),
                                       ("", False), ("0", False)])  # fmt: skip
def test_enabled_matches_the_orchestrator(jc, tmp_path, monkeypatch, value, on):
    (tmp_path / ".env").write_text(f"API_TOKEN=t\nCODER_ENABLED={value}\n")
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(tmp_path))
    assert jc.Config().enabled is on


def test_image_inputs_follow_coder_files_and_settings(jc, tmp_path, monkeypatch):
    (tmp_path / "coder").mkdir()
    (tmp_path / "orchestrator").mkdir()
    (tmp_path / "coder" / "Dockerfile").write_text("FROM x\n")
    (tmp_path / "orchestrator" / "requirements.txt").write_text("a\n")
    (tmp_path / "orchestrator" / "embedding_pin.py").write_text("b\n")
    (tmp_path / ".env").write_text("API_TOKEN=t\n")
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(tmp_path))
    first = jc.image_inputs(jc.Config())
    (tmp_path / "coder" / "settings.json").write_text("{}")
    second = jc.image_inputs(jc.Config())
    (tmp_path / ".env").write_text("API_TOKEN=t\nCODER_CONTEXT_WINDOW=65536\n")
    third = jc.image_inputs(jc.Config())
    assert len({first, second, third}) == 3


# --------------------------------------------------------------- network


def _net(**over):
    net = {
        "Internal": True,
        "EnableIPv6": False,
        "Options": {
            "com.docker.network.bridge.gateway_mode_ipv4": "isolated",
            "com.docker.network.bridge.name": "br-jesscoder",
        },
        "IPAM": {"Config": [{"Subnet": "10.213.250.0/24"}]},
        "Containers": {"a": {"Name": "brain-orchestrator"}, "b": {"Name": "jess-coder-demo"}},
    }
    net.update(over)
    return net


@pytest.mark.parametrize(
    "over, problem",
    [
        ({"Internal": False}, "not internal"),
        ({"Options": {"com.docker.network.bridge.name": "br-jesscoder"}}, "isolated"),
        ({"EnableIPv6": True}, "IPv6"),
        ({"Containers": {"a": {"Name": "brain-orchestrator"}, "c": {"Name": "searxng"}}}, "searxng"),
        ({"IPAM": {"Config": [{"Subnet": "10.9.0.0/24"}]}}, "subnet"),
    ],
)
def test_check_network_refuses_unsafe_layouts(jc, cfg, monkeypatch, over, problem):
    out = json.dumps([_net(**over)])
    monkeypatch.setattr(jc, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
    with pytest.raises(jc.Fatal, match=problem):
        jc.check_network(cfg)


def test_check_network_accepts_the_real_layout(jc, cfg, monkeypatch):
    out = json.dumps([_net()])
    monkeypatch.setattr(jc, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
    jc.check_network(cfg)


def test_network_refusal_is_recorded_as_a_failed_selftest(jc, cfg, monkeypatch, tmp_path):
    cfg.metrics_dir = tmp_path
    out = json.dumps([_net(Internal=False)])
    monkeypatch.setattr(jc, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": out})())
    with pytest.raises(jc.Fatal):
        jc.checked_network(cfg)
    assert "jess_coder_selftest_ok 0" in (tmp_path / "jess_coder_selftest.prom").read_text()


# ------------------------------------------------------------- patch: paths


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        ".Claude/settings.json",
        ".mcp.json",
        "CLAUDE.md",
        "claude.md",
        "CLAUDE.local.md",
        "QWEN.md",
        "scripts/jess-coder",
        "coder/Dockerfile",
        "docker-compose.yml",
        "monitoring/docker-compose.yml",
        "orchestrator/Dockerfile",
        "orchestrator/requirements.txt",
        "orchestrator/tests/conftest.py",
        "tts/llama-server-primary.service",
        "install.sh",
        "x/run.sh",
        ".env",
        "frontend/.env.local",
        "frontend/package.json",
        "frontend/eslint.config.mjs",
        "frontend/next.config.ts",
        "frontend/vitest.setup.ts",
        "frontend/.eslintrc.json",
        ".gitattributes",
        ".VSCode/tasks.json",
        "mypy.ini",
        ".husky/pre-commit",
        "../etc/passwd",
        "/abs",
        "a//b",
        ".git/config",
        "orchestrator/./x.py",
        "orchestrator/we ird.py",
        "orchestrator/x\n+++ b/y.py",
    ],
)
def test_refused_paths(jc, path):
    assert jc.refused_reason(path)


@pytest.mark.parametrize(
    "path",
    ["orchestrator/grocery_amounts.py", "orchestrator/tests/test_x.py", "docs/CODER.md", "frontend/src/app/page.tsx"],
)
def test_ordinary_paths(jc, path):
    assert jc.refused_reason(path) is None


def _entry(path, new_mode="100644", status="M", old_mode="100644"):
    return (old_mode, new_mode, status, path)


def test_review_flags_refused_path_and_allow_overrides(jc):
    e = [_entry(".github/workflows/ci.yml")]
    assert any("refused" in p for p in jc.review_changes(e, [], "", []))
    assert jc.review_changes(e, [], "", [".github/workflows/ci.yml"]) == []
    assert jc.review_changes(e, [], "", [".github/"]) == []


def test_review_flags_deleting_a_protected_file(jc):
    problems = jc.review_changes([_entry(".claude/settings.json", "000000", "D")], [], "", [])
    assert problems and "deletes" in problems[0]


@pytest.mark.parametrize("mode, word", [("100755", "executable"), ("120000", "symlink"), ("160000", "submodule")])
def test_review_flags_modes(jc, mode, word):
    problems = jc.review_changes([_entry("orchestrator/x", mode, "A", "000000")], [], "", [])
    assert any(word in p for p in problems)


def test_review_flags_binary(jc):
    problems = jc.review_changes([_entry("i.png", status="A")], ["i.png"], "", [])
    assert any("binary" in p for p in problems)


def test_parse_raw(jc):
    raw = (
        b":100644 100755 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0run.py\0"
        b":000000 120000 " + b"0" * 40 + b" " + b"c" * 40 + b" A\0link\0"
    )
    assert jc.parse_raw(raw) == [("100644", "100755", "M", "run.py"), ("000000", "120000", "A", "link")]
    with pytest.raises(ValueError):
        jc.parse_raw(b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" R100\0x\0y\0")


# --------------------------------------------------------- patch: characters


def _diff(path: str, body: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


@pytest.mark.parametrize(
    "line, what",
    [
        ("+print('\x1b]52;c;ZWNobyBoaQ==\x07')", "control character 0x1b"),
        ("+access = 'user‮ ⁦// admin⁩ ⁦'", "bidirectional"),
        ("+x = 1\x08\x08", "control character 0x08"),
        ("+x = '⁡'", "invisible character"),
        ("+x = 'a​b'", "invisible character U+200B"),
        ("+// ok alert(1)", "invisible character U+2028"),
        ("+ㅤ = 1", "invisible character U+3164"),
    ],
)
def test_review_flags_terminal_tricks(jc, line, what):
    problems = jc.text_problems(_diff("a.py", line + "\n"))
    assert problems and what in problems[0]


def test_only_added_lines_are_judged(jc):
    # Context and removed lines are the trusted base (existing files hold ZWSP / ZWJ).
    assert jc.text_problems(_diff("a.py", " x = 'a​b'\n-y = '​'\n+z = 1\n")) == []


def test_plain_text_and_emoji_pass(jc):
    assert jc.text_problems(_diff("a.py", "+x = 'café \U0001f469‍\U0001f4bb'  # tab\tok\r\n")) == []


def test_sanitize_for_terminal(jc):
    out = jc.sanitize_for_terminal("a\x1b[2Jb‮c\td\n")
    assert out == "a^[[2Jb<U+202E>c\td\n"
    assert jc.sanitize_for_terminal("x\ny", keep_newlines=False) == "x^Jy"


# ------------------------------------- patch: hostile diff text vs trusted git


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def base_bundle(tmp_path):
    """A repo with a.py at a base commit, as a bundle (like jess-coder's)."""
    repo = tmp_path / "src"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.py").write_text("def f(x):\n    return x\n")
    _git("add", "a.py", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base", cwd=repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    bundle = tmp_path / "base.bundle"
    _git("bundle", "create", "-q", str(bundle), "main", cwd=repo)
    return bundle, base


def _new_file(path: str, mode_line: str, body: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\n{mode_line}\nindex 0000000..1111111\n--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1 @@\n+{body}\n\\ No newline at end of file\n"
    ).encode()


@needs_git
@pytest.mark.parametrize(
    "mode_line",
    [
        "new file mode 120000\r",
        "new file mode 0120000",
        "new file mode 120000 ",
        "new file mode  120000",
    ],
    ids=["cr", "leading-zero", "trailing-space", "two-spaces"],
)
def test_odd_mode_lines_cant_hide_a_symlink(jc, base_bundle, mode_line):
    bundle, base = base_bundle
    raw = _new_file("orchestrator/link.py", mode_line, "../.env")
    try:
        entries, _stats, _text, _canon, problems = jc.check_box_diff(bundle, base, raw, [])
    except jc.Fatal:
        return  # git refused it outright: also fine
    assert entries == [("000000", "120000", "A", "orchestrator/link.py")]
    assert any("symlink" in p for p in problems)


@needs_git
@pytest.mark.parametrize("mode_line", ["new file mode 100755\r", "new file mode 755"], ids=["cr", "short"])
def test_odd_mode_lines_cant_hide_an_executable(jc, base_bundle, mode_line):
    bundle, base = base_bundle
    try:
        _e, _s, _t, _c, problems = jc.check_box_diff(bundle, base, _new_file("run_me.py", mode_line, "x"), [])
    except jc.Fatal:
        return
    assert any("executable" in p for p in problems)


@needs_git
def test_decoy_hunk_lines_are_not_what_you_review(jc, base_bundle):
    bundle, base = base_bundle
    raw = (
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n"
        b" def f(x):\n+    y = 1\n     return x\n+    check_auth(x)\n"
    )
    _e, _s, text, canonical, _p = jc.check_box_diff(bundle, base, raw, [])
    assert "check_auth" not in text  # git ignored it, so the review doesn't show it
    assert b"+    y = 1" in canonical


@needs_git
def test_no_newline_join_is_shown_as_applied(jc, base_bundle):
    bundle, base = base_bundle
    raw = (
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,4 @@\n def f(x):\n"
        b"+    # validate the caller\n\\ No newline at end of file\n+    check_auth(x)\n     return x\n"
    )
    try:
        _e, _s, text, _c, _p = jc.check_box_diff(bundle, base, raw, [])
    except jc.Fatal:
        return
    added = [line for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]
    assert "+    check_auth(x)" not in added  # what the box displayed is not what git applied


@needs_git
def test_forged_rename_shows_the_deletion(jc, base_bundle, tmp_path):
    bundle, base = base_bundle
    raw = b"diff --git a/a.py b/CLAUDE.md\nsimilarity index 100%\nrename from a.py\nrename to CLAUDE.md\n"
    _e, _s, _t, _c, problems = jc.check_box_diff(bundle, base, raw, [])
    assert any("CLAUDE.md" in p and "refused" in p for p in problems)


@needs_git
def test_ordinary_change_passes(jc, base_bundle):
    bundle, base = base_bundle
    raw = b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def f(x):\n-    return x\n+    return x + 1\n"
    entries, stats, text, canonical, problems = jc.check_box_diff(bundle, base, raw, [])
    assert problems == [] and entries == [("100644", "100644", "M", "a.py")]
    assert stats == [("1", "1", "a.py")] and "+    return x + 1" in text


@needs_git
def test_patch_that_does_not_apply_is_refused(jc, base_bundle):
    bundle, base = base_bundle
    raw = (
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def g(y):\n-    return y\n+    return 2\n"
    )
    with pytest.raises(jc.Fatal, match="does not apply"):
        jc.check_box_diff(bundle, base, raw, [])


# -------------------------------------------------------------- entrypoint


def test_exclude_tools_keeps_only_the_working_set(entry):
    excluded = set(entry.EXCLUDE_TOOLS.split(","))
    for kept in ("read_file", "write_file", "edit", "glob", "grep_search", "run_shell_command"):
        assert kept not in excluded
    # Network tools, subagents, memory, and the interactive tool whose schema
    # the gate refuses (propose_goal: maxLength 1500).
    for gone in ("web_fetch", "web_search", "agent", "task", "save_memory", "tool_search", "propose_goal"):
        assert gone in excluded


def test_entrypoint_and_launcher_agree_on_selftest_exit_code(jc, entry):
    assert entry.SELFTEST_FAILED == jc.SELFTEST_FAILED


def test_patch_mode_never_uses_the_workspace_git_dir():
    src = (_CODER / "entrypoint.py").read_text()
    body = src[src.index("def patch(") : src.index("def _rmtree(")]
    assert '"GIT_DIR": str(gitdir)' in body and '"GIT_CONFIG_GLOBAL": "/dev/null"' in body
    assert "alternates" in body


def test_entrypoint_names_are_full_matches(entry):
    assert entry.NAME_RE.fullmatch("abc\n") is None and entry.SHA_RE.fullmatch("a" * 40 + "\n") is None


@needs_git
def test_patch_box_ignores_workspace_filter_drivers(entry, tmp_path, monkeypatch):
    """A clean filter planted in the workspace's own .git/config must not run."""
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    _git("init", "-q", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "b", cwd=repo)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    pwned = tmp_path / "pwned"
    _git("config", "filter.x.clean", f"sh -c 'touch {pwned}; cat'", cwd=repo)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
    (repo / "f.txt").write_text("hi\n")
    monkeypatch.setattr(entry, "WORK", work)
    monkeypatch.setattr(entry, "REPO", repo)
    monkeypatch.setattr(entry.tempfile, "tempdir", str(tmp_path))
    out = tmp_path / "out"
    with out.open("wb") as fh:
        monkeypatch.setattr(sys, "stdout", type("O", (), {"buffer": fh})())
        entry.patch([base])
    assert not pwned.exists()
    assert b"+hi" in out.read_bytes()


# ---------------------------------------------------------------- selftest


def test_selftest_tcp_probe_classifies_answers(selftest):
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert selftest._tcp_reach(("127.0.0.1", port, socket.AF_INET, "closed")) == f"127.0.0.1:{port} connected"
    # Closed again: a refusal is fine only where it is expected (the gate host).
    assert selftest._tcp_reach(("127.0.0.1", port, socket.AF_INET, "closed")) is None
    hit = selftest._tcp_reach(("127.0.0.1", port, socket.AF_INET, "no_route"))
    assert hit and "ECONNREFUSED" in hit


def test_selftest_parse_hosts(selftest):
    assert selftest._parse_hosts("10.0.0.1, 127.0.0.1,bad,10.0.0.1,,172.17.0.1") == ["10.0.0.1", "172.17.0.1"]
    assert len(selftest._parse_hosts(",".join(f"10.1.{i // 250}.{i % 250 + 1}" for i in range(300)))) == 300


def test_selftest_parse_link_local(selftest):
    assert selftest._parse_link_local("fe80::1,2001:db8::1,junk") == ["fe80::1%eth0"]


def test_selftest_reads_gate_from_settings(selftest, tmp_path, monkeypatch):
    s = _settings()
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(s))
    monkeypatch.setattr(selftest, "SETTINGS", str(path))
    assert selftest._gate_from_settings() == ("10.213.250.2", 8888)
    s["modelProviders"]["openai"][0]["baseUrl"] = "https://example.com/v1"
    path.write_text(json.dumps(s))
    with pytest.raises(ValueError):
        selftest._gate_from_settings()
