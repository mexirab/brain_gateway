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
- the self-test's probes tell "no route" from "something answered";
- the launcher refuses before any box or token exists, always kills its box and
  revokes its token, maps box exit codes to the self-test metric, and runs its
  helper boxes offline; the entrypoint only ever shows a complete workspace;
  the self-test parsers and expectations fail closed.

Docker, the orchestrator API and the box process are stand-ins in the launcher
tests: nothing here starts a container or reaches a network.

Needs a full checkout (coder/, scripts/); skips otherwise, like
test_coder_hardening.py. The git-backed patch tests also need git.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import io
import ipaddress
import itertools
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from orchestrator.tests._helpers import REPO_ROOT, load_script

_REPO = REPO_ROOT
_CODER = _REPO / "coder"
_LAUNCHER = _REPO / "scripts" / "jess-coder"

pytestmark = pytest.mark.skipif(
    not (_CODER / "Dockerfile").is_file() or not _LAUNCHER.is_file(),
    reason="needs a full repo checkout (coder/, scripts/jess-coder)",
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(scope="module")
def jc() -> ModuleType:
    return load_script(_LAUNCHER, "jess_coder_launcher")


@pytest.fixture(scope="module")
def entry() -> ModuleType:
    return load_script(_CODER / "entrypoint.py", "jess_coder_entrypoint")


@pytest.fixture(scope="module")
def selftest() -> ModuleType:
    return load_script(_CODER / "selftest.py", "jess_coder_selftest")


@pytest.fixture
def cfg(jc, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("API_TOKEN=t\nCODER_ENABLED=true\n")
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(tmp_path))
    for key in ("JESS_CODER_IMAGE", "JESS_CODER_NETWORK", "JESS_CODER_GATE_CONTAINER", "JESS_CODER_ALLOW_UNMOUNTED"):
        monkeypatch.delenv(key, raising=False)
    # Never the real /srv/jess-coder or node_exporter collector, even on Jupiter.
    monkeypatch.setenv("JESS_CODER_HOME", str(tmp_path / "no-home"))
    monkeypatch.setenv("JESS_CODER_METRICS_DIR", str(tmp_path / "no-metrics"))
    return jc.Config()


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


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
    monkeypatch.setattr(jc, "run", lambda *a, **k: _completed(out))
    with pytest.raises(jc.Fatal, match=problem):
        jc.check_network(cfg)


def test_check_network_accepts_the_real_layout(jc, cfg, monkeypatch):
    out = json.dumps([_net()])
    monkeypatch.setattr(jc, "run", lambda *a, **k: _completed(out))
    jc.check_network(cfg)


def test_network_refusal_is_recorded_as_a_failed_selftest(jc, cfg, monkeypatch, tmp_path):
    cfg.metrics_dir = tmp_path
    out = json.dumps([_net(Internal=False)])
    monkeypatch.setattr(jc, "run", lambda *a, **k: _completed(out))
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
        # case doesn't matter
        "SCRIPTS/deploy.py",
        ".GITHUB/workflows/x.yml",
        "Docker-Compose.Override.YML",
        "orchestrator/DOCKERFILE.dev",
        "orchestrator/Requirements-Dev.TXT",
        "frontend/Package.JSON",
        "x/RUN.SH",
        ".ENV.production",
        ".GIT/config",
        ".Git",
        "MakeFile",
        "docs/Claude.MD",
        "frontend/AGENTS.md",
        "frontend/Vite.Config.MTS",
        "frontend/.ESLintRC.cjs",
        "Monitoring/x.yml",
        "frontend/src/app/(private)/.env",
        # agent config dirs anywhere in the tree
        "frontend/.claude/skills/x/SKILL.md",
        "orchestrator/.claude/settings.json",
    ],
)
def test_refused_paths(jc, path):
    assert jc.refused_reason(path)


@pytest.mark.parametrize(
    "path",
    [
        "orchestrator/grocery_amounts.py",
        "orchestrator/tests/test_x.py",
        "docs/CODER.md",
        "frontend/src/app/page.tsx",
        # lookalikes of refused names, and Next.js route folders
        "orchestrator/scripts_helper.py",
        "orchestrator/environment.py",
        "orchestrator/tests/test_dockerfile.py",
        "frontend/src/config.ts",
        "docs/claude_notes.txt",
        "frontend/src/app/(private)/settings/page.tsx",
        "frontend/src/app/api/[id]/route.ts",
    ],
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
        ("+access = 'user\u202e \u2066// admin\u2069 \u2066'", "bidirectional"),
        ("+x = 1\x08\x08", "control character 0x08"),
        ("+x = '\u2061'", "invisible character"),
        ("+x = 'a\u200bb'", "invisible character U+200B"),
        ("+// ok\u2028alert(1)", "invisible character U+2028"),
        ("+\u3164 = 1", "invisible character U+3164"),
    ],
)
def test_review_flags_terminal_tricks(jc, line, what):
    problems = jc.text_problems(_diff("a.py", line + "\n"))
    assert problems and what in problems[0]


def test_only_added_lines_are_judged(jc):
    # Context and removed lines are the trusted base (existing files hold ZWSP / ZWJ).
    assert jc.text_problems(_diff("a.py", " x = 'a\u200bb'\n-y = '\u200b'\n+z = 1\n")) == []


def test_plain_text_and_emoji_pass(jc):
    assert jc.text_problems(_diff("a.py", "+x = 'caf\u00e9 \U0001f469\u200d\U0001f4bb'  # tab\tok\r\n")) == []


def test_sanitize_for_terminal(jc):
    out = jc.sanitize_for_terminal("a\x1b[2Jb\u202ec\td\n")
    assert out == "a^[[2Jb<U+202E>c\td\n"
    assert jc.sanitize_for_terminal("x\ny", keep_newlines=False) == "x^Jy"
    assert jc.sanitize_for_terminal("a\x7fb\x85c\u200bd\u2028e") == "a^?b<U+0085>c<U+200B>d<U+2028>e"


# ------------------------------------- patch: hostile diff text vs trusted git


@pytest.fixture(scope="module")
def base_bundle(tmp_path_factory):
    """A repo with a.py at a base commit, as a bundle (like jess-coder's).
    Module-scoped: tests only read the bundle (or copy it)."""
    tmp_path = tmp_path_factory.mktemp("base-bundle")
    repo = tmp_path / "src"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.py").write_text("def f(x):\n    return x\n")
    _git("add", "a.py", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base", cwd=repo)
    base = _git_out("rev-parse", "HEAD", cwd=repo)
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


@pytest.mark.parametrize(
    "header",
    [
        b"similarity index 100%\nrename from a.py\nrename to CLAUDE.md",
        b"copy from a.py\ncopy to CLAUDE.md",
        b"dissimilarity index 90%",
        b"GIT binary patch\nliteral 999999999",
        b"GIT binary patch\ndelta 12",
    ],
    ids=["rename", "copy", "dissimilarity", "binary-literal", "binary-delta"],
)
def test_forged_headers_are_refused_before_git_sees_them(jc, tmp_path, header):
    """The patch box never makes these (--no-renames, no --binary): one in its output is forged."""
    raw = b"diff --git a/a.py b/CLAUDE.md\n" + header + b"\n"
    with pytest.raises(jc.Fatal, match="forged"):
        jc.check_box_diff(tmp_path / "never-opened.bundle", "a" * 40, raw, [])


@needs_git
def test_header_words_on_added_lines_are_just_text(jc, base_bundle):
    bundle, base = base_bundle
    raw = (
        b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n def f(x):\n"
        b"+    # rename from old; copy from x; literal 1\n     return x\n"
    )
    _e, _s, text, _c, problems = jc.check_box_diff(bundle, base, raw, [])
    assert problems == [] and "+    # rename from old; copy from x; literal 1" in text


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
    base = _git_out("rev-parse", "HEAD", cwd=repo)
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


# ====================================================================== launcher
# Docker, the orchestrator API and the box process are stand-ins from here on.

BASE = "a" * 40
TID = "0123456789abcdef" * 2


def _boom(*_a, **_k):
    raise AssertionError("must not be called")


def _fake_subprocess(**over) -> types.SimpleNamespace:
    """Stand-in for the launcher's ``subprocess`` module: the real names, some replaced."""
    names = ("PIPE", "DEVNULL", "STDOUT", "run", "Popen", "CalledProcessError", "CompletedProcess", "TimeoutExpired")
    fake = types.SimpleNamespace(**{n: getattr(subprocess, n) for n in names})
    for key, value in over.items():
        setattr(fake, key, value)
    return fake


class _Api:
    """Records the launcher's API calls; answers from a {(method, path): (status, body)} table."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, cfg, method, path, body=None, *, auth=True):
        self.calls.append((method, path, body))
        answer = self.table.get((method, path), (200, {}))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self, method):
        return [p for m, p, _ in self.calls if m == method]


@pytest.fixture
def home(jc, cfg, tmp_path, monkeypatch):
    """cfg with a coder home (lab override: not its own mount) and a metrics dir, both in tmp_path."""
    cfg.home = tmp_path / "home"
    cfg.home.mkdir()
    cfg.metrics_dir = tmp_path / "metrics"
    cfg.metrics_dir.mkdir()
    monkeypatch.setenv("JESS_CODER_ALLOW_UNMOUNTED", "1")
    jc.check_home(cfg)
    return cfg


def _write_session(cfg, name="demo", base=BASE) -> Path:
    sdir = cfg.sessions / name
    (sdir / "box").mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"name": name, "base": base}))
    return sdir


def _metric(cfg):
    """jess_coder_selftest_ok as written, or None if no metric file."""
    path = cfg.metrics_dir / "jess_coder_selftest.prom"
    if not path.exists():
        return None
    m = re.search(r"^jess_coder_selftest_ok (\d)$", path.read_text(), re.M)
    return m.group(1) if m else "malformed"


# ------------------------------------------------------ launcher: run_captured


def _py(code: str) -> list:
    return [sys.executable, "-c", code]


@pytest.fixture
def kills(jc, monkeypatch):
    seen = []
    monkeypatch.setattr(jc, "kill_cid", seen.append)
    return seen


def test_run_captured_returns_output_and_code(jc, tmp_path, kills):
    code, out, err, problem = jc.run_captured(
        _py(
            "import os, sys; sys.stdout.write(repr(sys.stdin.read()) + os.environ['MARK']); sys.stderr.write('w'); sys.exit(3)"
        ),
        tmp_path / "cid",
        limit=1024,
        timeout=60,
        env={**os.environ, "MARK": "!"},
    )
    # stdin is /dev/null: a helper box never gets the terminal
    assert (code, out, err, problem) == (3, b"''!", b"w", "")
    assert kills == []


@pytest.mark.parametrize("with_cid", [True, False], ids=["box", "host-git"])
def test_run_captured_stops_output_over_the_limit(jc, tmp_path, kills, with_cid):
    cid = tmp_path / "cid" if with_cid else None
    code, out, _err, problem = jc.run_captured(
        _py("import sys\nwhile True:\n    sys.stdout.buffer.write(b'x' * 65536)"), cid, limit=100_000, timeout=60
    )
    assert problem == "too_large" and code != 0  # killed, not finished
    assert 100_000 < len(out) <= 100_000 + 65536
    assert kills == ([cid] if with_cid else [])


def test_run_captured_kills_at_the_deadline_and_keeps_what_came(jc, tmp_path, kills):
    t0 = time.monotonic()
    code, out, _err, problem = jc.run_captured(
        _py("import time; print('started', flush=True); time.sleep(60)"), tmp_path / "cid", limit=1024, timeout=1
    )
    assert problem == "timeout" and code != 0 and kills == [tmp_path / "cid"]
    assert out == b"started\n"
    assert time.monotonic() - t0 < 30


def test_run_captured_keeps_only_the_stderr_tail(jc, tmp_path, kills):
    code, out, err, problem = jc.run_captured(
        _py("import sys; sys.stderr.write('a' * 10000 + 'END'); print('ok')"), tmp_path / "cid", limit=1024, timeout=60
    )
    assert (code, out, problem) == (0, b"ok\n", "")
    assert len(err) == 4096 and err.endswith(b"END")


def test_kill_cid_kills_by_container_id_only(jc, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jc, "run", lambda cmd, **k: calls.append(cmd))
    cid = "f" * 64
    files = {"missing": None, "name": "jess-coder-demo\n", "short": "f" * 12, "upper": "F" * 64, "good": cid + "\n"}
    for name, text in files.items():
        if text is not None:
            (tmp_path / name).write_text(text)
        jc.kill_cid(tmp_path / name)
    assert calls == [["docker", "kill", cid]]


def test_reap_overdue_kills_expired_boxes_by_id(jc, monkeypatch):
    now = time.time()
    past, future, in_grace = int(now - jc.KILL_GRACE_S - 60), int(now + 3600), int(now - 30)
    rows = [
        ("a" * 12, "jess-coder-old", past),
        ("b" * 12, "jess-coder-live", future),
        ("c" * 12, "jess-coder-odd", "soon"),
        ("d" * 12, "searxng", past),
        ("not-an-id", "jess-coder-badid", past),
        ("e" * 12, "jess-coder-grace", in_grace),
        ("f" * 12, "jess-coder-nolabel", ""),  # docker prints an empty field for a missing label
        ("1" * 12, "jess-coder-nan", "nan"),
        ("2" * 12, "jess-coder-inf", "inf"),
    ]
    listing = ["\t".join(map(str, r)) for r in rows]
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return _completed("\n".join(listing) + "\n")

    monkeypatch.setattr(jc, "run", fake_run)
    assert jc.reap_overdue() == [
        "jess-coder-old", "jess-coder-odd", "jess-coder-nolabel", "jess-coder-nan", "jess-coder-inf",
    ]  # fmt: skip
    assert "label=jess-coder=1" in calls[0]
    assert [c[2] for c in calls[1:]] == ["a" * 12, "c" * 12, "f" * 12, "1" * 12, "2" * 12]


def test_running_boxes_filters_by_label_and_prefix(jc, monkeypatch):
    seen = []

    def fake_run(cmd, **k):
        seen.append(cmd)
        return _completed("jess-coder-a\nsearxng\njess-coder-b\n")

    monkeypatch.setattr(jc, "run", fake_run)
    assert jc.running_boxes("session") == ["jess-coder-a", "jess-coder-b"]
    assert "label=jess-coder.kind=session" in seen[0]
    jc.running_boxes()
    assert "label=jess-coder=1" in seen[1]


def test_box_command_carries_an_expiry_label(jc, cfg, tmp_path):
    cmd = jc.box_command(cfg, name="demo", kind="session", mode=["session"], network="none", cidfile=tmp_path / "c",
                         expires=1_900_000_000.7)  # fmt: skip
    assert "jess-coder.expires=1900000000" in _flag_value(cmd, "--label")
    labels = _flag_value(
        jc.box_command(cfg, name="x", kind="rm", mode=["clean"], network="none", cidfile=tmp_path), "--label"
    )
    (default,) = [v for v in labels if v.startswith("jess-coder.expires=")]
    assert abs(int(default.split("=")[1]) - (time.time() + 3600)) < 60  # a helper box without one still expires


# ------------------------------------------------------- launcher: preflight


def test_check_home_refuses_a_missing_dir(jc, cfg):
    with pytest.raises(jc.Fatal, match="does not exist"):
        jc.check_home(cfg)


def test_check_home_refuses_the_root_filesystem(jc, cfg, tmp_path, monkeypatch):
    cfg.home = tmp_path
    monkeypatch.setattr(jc.os.path, "ismount", lambda p: False)
    with pytest.raises(jc.Fatal, match="not its own filesystem"):
        jc.check_home(cfg)
    assert not cfg.sessions.exists()


@pytest.mark.parametrize(
    "opts, missing",
    [
        ("rw,relatime", "nosuid,nodev"),
        ("rw,nosuid,relatime", "nodev"),
        ("rw,nosuidx,nodev", "nosuid"),
        ("", "nosuid,nodev"),
    ],
    ids=["neither", "no-nodev", "lookalike", "findmnt-failed"],
)
def test_check_home_wants_nosuid_and_nodev(jc, cfg, tmp_path, monkeypatch, opts, missing):
    cfg.home = tmp_path
    monkeypatch.setattr(jc.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed(opts + "\n"))
    with pytest.raises(jc.Fatal, match=f"without {missing} "):
        jc.check_home(cfg)


def test_check_home_makes_private_dirs_on_a_good_mount(jc, cfg, tmp_path, monkeypatch):
    cfg.home = tmp_path
    asked = []
    monkeypatch.setattr(jc.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(jc, "run", lambda cmd, **k: asked.append(cmd) or _completed("rw,nosuid,nodev,relatime\n"))
    jc.check_home(cfg)
    assert asked == [["findmnt", "-n", "-o", "OPTIONS", str(tmp_path)]]
    for d in (cfg.sessions, cfg.bundles, cfg.patches):
        assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700


def test_check_home_lab_override_is_exactly_1(jc, cfg, tmp_path, monkeypatch):
    cfg.home = tmp_path
    monkeypatch.setenv("JESS_CODER_ALLOW_UNMOUNTED", "1")
    monkeypatch.setattr(jc.os.path, "ismount", _boom)
    monkeypatch.setattr(jc, "run", _boom)
    jc.check_home(cfg)
    assert cfg.sessions.is_dir()
    monkeypatch.setenv("JESS_CODER_ALLOW_UNMOUNTED", "yes")
    monkeypatch.setattr(jc.os.path, "ismount", lambda p: False)
    with pytest.raises(jc.Fatal, match="not its own filesystem"):
        jc.check_home(cfg)


@pytest.mark.parametrize(
    "label, lab, problem",
    [(None, False, "not built yet"), (None, True, "not built yet"), ("old", False, "out of date"), ("old", True, None),
     ("want", False, None)],
    ids=["missing", "missing-lab", "stale", "stale-lab", "current"],
)  # fmt: skip
def test_check_image(jc, cfg, monkeypatch, label, lab, problem):
    cfg.lab = lab
    monkeypatch.setattr(jc, "image_label", lambda c: label)
    monkeypatch.setattr(jc, "image_inputs", lambda c: "want")
    if problem is None:
        jc.check_image(cfg)
    else:
        with pytest.raises(jc.Fatal, match=problem):
            jc.check_image(cfg)


def test_image_label_reads_the_inputs_label(jc, cfg, monkeypatch):
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed("abc123\n"))
    assert jc.image_label(cfg) == "abc123"
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed("", returncode=1))
    assert jc.image_label(cfg) is None


def test_selftest_metric_is_written_whole(jc, home):
    jc.write_selftest_metric(home, True)
    path = home.metrics_dir / "jess_coder_selftest.prom"
    text = path.read_text()
    assert _metric(home) == "1"
    stamp = re.search(r"^jess_coder_selftest_last_run_timestamp_seconds (\d+)$", text, re.M)
    assert stamp and abs(int(stamp.group(1)) - time.time()) < 60
    assert all(line.startswith("# ") or re.fullmatch(r"[a-z_]+ \d+", line) for line in text.splitlines())
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # node_exporter runs as another user
    jc.write_selftest_metric(home, False)
    assert _metric(home) == "0"
    assert [p.name for p in home.metrics_dir.iterdir()] == ["jess_coder_selftest.prom"]  # no temp files left


def test_selftest_metric_skips_a_missing_collector(jc, cfg):
    jc.write_selftest_metric(cfg, True)
    assert not cfg.metrics_dir.exists()


def test_selftest_metric_failure_is_a_warning(jc, home, capsys):
    (home.metrics_dir / "jess_coder_selftest.prom").mkdir()  # the rename onto it fails
    jc.write_selftest_metric(home, True)
    assert "self-test metric not written" in capsys.readouterr().out


def test_lab_runs_leave_the_production_metric_alone(jc, tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("API_TOKEN=t\n")
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(tmp_path))
    monkeypatch.setenv("JESS_CODER_IMAGE", "jess-coder:lab")
    monkeypatch.delenv("JESS_CODER_NETWORK", raising=False)
    monkeypatch.delenv("JESS_CODER_METRICS_DIR", raising=False)
    lab = jc.Config()
    assert lab.lab
    lab.metrics_dir = tmp_path / "metrics"
    lab.metrics_dir.mkdir()
    jc.write_selftest_metric(lab, False)
    assert list(lab.metrics_dir.iterdir()) == []
    monkeypatch.setenv("JESS_CODER_METRICS_DIR", str(lab.metrics_dir))  # asked for explicitly
    jc.write_selftest_metric(lab, False)
    assert _metric(lab) == "0"


def test_selftest_hosts_cover_this_host_and_the_lan(jc, cfg, monkeypatch):
    answers = {
        ("ip", "-j", "-4", "addr", "show"): json.dumps(
            [
                {"addr_info": [{"local": "127.0.0.1"}, {"local": "192.0.2.10"}, {"local": "172.17.0.1"}]},
                {"addr_info": [{"local": "192.0.2.10"}, {}]},
            ]
        ),
        ("ip", "-j", "-4", "route", "show", "default"): json.dumps([{"gateway": "192.0.2.1"}, {"dst": "default"}]),
    }
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed(answers.get(tuple(cmd), "[]")))
    cfg.env.update(
        NODE_HELIOS_IP="192.0.2.20",
        NODE_SATURN_IP="saturn.lan",
        MODEL_URL="http://192.0.2.20:8080/v1",
        HA_URL="http://192.0.2.10:8123",
        STT_URL="http://helios.lan:8003",
        PAPERLESS_URL="http://198.51.100.7:8777",
    )
    assert jc.selftest_hosts(cfg) == ["192.0.2.10", "172.17.0.1", "192.0.2.1", "192.0.2.20", "198.51.100.7"]
    env = jc.selftest_env(cfg)
    assert env["JESS_SELFTEST_HOSTS"] == "192.0.2.10,172.17.0.1,192.0.2.1,192.0.2.20,198.51.100.7"
    assert env["JESS_CODER_UID"] == str(jc.BOX_UID) and env["JESS_CODER_BOX_RANGE"] == cfg.box_range


def test_bridge_link_local(jc, cfg, monkeypatch):
    out = json.dumps([{"addr_info": [{"local": "fe80::1"}, {"local": "2001:db8::1"}]}])
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed(out))
    assert jc.bridge_link_local(cfg) == ["fe80::1"]
    monkeypatch.setattr(jc, "run", lambda cmd, **k: _completed("", returncode=1))
    assert jc.bridge_link_local(cfg) == []
    cfg.network = "lab-net"  # not our bridge: nothing to ask
    monkeypatch.setattr(jc, "run", _boom)
    assert jc.bridge_link_local(cfg) == []


# -------------------------------------------------------- launcher: sessions


@pytest.mark.parametrize(
    "text",
    ['{"base": "../../etc"}', '{"base": "' + "A" * 40 + '"}', '{"base": 7}', '["x"]', "not json", '{"name": "x"}'],
    ids=["traversal", "upper", "number", "list", "not-json", "no-base"],
)
def test_read_meta_refuses_bad_files(jc, home, text):
    sdir = home.sessions / "demo"
    sdir.mkdir()
    (sdir / "meta.json").write_text(text)
    assert jc.read_meta(home, "demo") is None


def test_read_meta_and_session_names(jc, home):
    assert jc.read_meta(home, "demo") is None
    _write_session(home, "demo")
    assert jc.read_meta(home, "demo")["base"] == BASE
    for odd in ("Bad", ".hidden", "x" * 40):
        (home.sessions / odd).mkdir()
    (home.sessions / "file").write_text("")
    assert jc.list_session_names(home) == ["demo"]


def test_prune_bundles_keeps_used_and_recent(jc, home):
    a, b, c, d, e = (ch * 40 for ch in "abcde")
    _write_session(home, "one", a)
    broken = home.sessions / "broken"
    broken.mkdir()
    (broken / "meta.json").write_text('{"base": "' + b + '", "x": ')  # unreadable: its base is not in use
    old = time.time() - 7200
    for sha in (a, b, c, d):
        f = home.bundles / f"{sha}.bundle"
        f.write_bytes(b"x")
        os.utime(f, (old, old))
    (home.bundles / f"{e}.bundle").write_bytes(b"x")  # a concurrent start may not have written meta yet
    notes = home.bundles / "notes.txt"
    notes.write_text("x")
    os.utime(notes, (old, old))
    jc.prune_bundles(home, keep=d)
    assert sorted(p.name for p in home.bundles.iterdir()) == sorted(
        [f"{a}.bundle", f"{d}.bundle", f"{e}.bundle", "notes.txt"]
    )


def test_revoke_by_known_id(jc, cfg, monkeypatch):
    api = _Api()
    monkeypatch.setattr(jc, "api", api)
    jc.revoke_session_tokens(cfg, "demo", TID)
    assert [(m, p) for m, p, _ in api.calls] == [("DELETE", f"/api/coder/sessions/{TID}")]


def test_revoke_falls_back_to_the_session_label(jc, cfg, monkeypatch):
    one, two = "1" * 32, "2" * 32
    sessions = [
        {"id": one, "label": "demo"},
        {"id": "f" * 32, "label": "demo-2"},
        {"id": "../../health", "label": "demo"},
        {"label": "demo"},
        {"id": two, "label": "demo"},
    ]
    api = _Api({("GET", "/api/coder/sessions"): (200, {"sessions": sessions})})
    monkeypatch.setattr(jc, "api", api)
    jc.revoke_session_tokens(cfg, "demo", None)
    assert api.paths("DELETE") == [f"/api/coder/sessions/{one}", f"/api/coder/sessions/{two}"]


def test_revoke_revokes_nothing_when_the_list_fails(jc, cfg, monkeypatch):
    api = _Api({("GET", "/api/coder/sessions"): (500, {"sessions": [{"id": TID, "label": "demo"}]})})
    monkeypatch.setattr(jc, "api", api)
    jc.revoke_session_tokens(cfg, "demo", "")
    assert api.paths("DELETE") == []


@pytest.mark.parametrize("status, warned", [(200, False), (404, False), (500, True)])
def test_revoke_warns_when_a_token_stays_live(jc, cfg, monkeypatch, capsys, status, warned):
    monkeypatch.setattr(jc, "api", _Api({("DELETE", f"/api/coder/sessions/{TID}"): (status, {})}))
    jc.revoke_session_tokens(cfg, "demo", TID)
    assert ("not revoked" in capsys.readouterr().out) is warned


def test_relay_headless_output_decodes_across_chunks_and_is_terminal_safe(jc, monkeypatch, capsys):
    chunks = [b"caf\xc3", b"\xa9 \x1b[2J\xc2\x9b", b""]
    monkeypatch.setattr(jc, "os", types.SimpleNamespace(read=lambda fd, n: chunks.pop(0)))
    jc.relay_headless_output(5)
    assert capsys.readouterr().out == "caf\u00e9 ^[[2J<U+009B>"  # one split character, not two U+FFFD


def test_relay_headless_output_caps_what_it_shows_but_drains_the_box(jc, monkeypatch, capsys):
    chunks = [b"a" * 8, b"b" * 8, b"c" * 8, b"d" * 8, b""]
    monkeypatch.setattr(jc, "os", types.SimpleNamespace(read=lambda fd, n: chunks.pop(0)))
    monkeypatch.setattr(jc, "MAX_HEADLESS_OUTPUT", 10)
    jc.relay_headless_output(5)
    shown, _, notice = capsys.readouterr().out.partition("\n")
    assert shown == "a" * 8 + "b" * 8 and "not shown" in notice and "c" * 8 not in notice
    assert chunks == []  # read to the end: a box that keeps printing never blocks


class _Opener:
    """urllib opener stand-in: answers /health from a list of up/down states."""

    def __init__(self, states, urls):
        self.states, self.urls = states, urls

    def open(self, url, timeout):
        self.urls.append(url)
        if not self.states.pop(0):
            raise OSError("down")
        return contextlib.nullcontext(types.SimpleNamespace(status=200))


@pytest.fixture
def brain(jc, cfg, monkeypatch):
    """ensure_brain with the brain's /health, the wake API and the clock stubbed out."""
    st = types.SimpleNamespace(states=[], urls=[], sleeps=0, api=_Api(), cfg=cfg)
    cfg.env["MODEL_URL"] = "http://192.0.2.20:8080/v1"
    clock = itertools.count(0, 100)

    def sleep(_s):
        st.sleeps += 1

    monkeypatch.setattr(jc.urllib.request, "build_opener", lambda *h: _Opener(st.states, st.urls))
    monkeypatch.setattr(jc, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=sleep))
    monkeypatch.setattr(jc, "api", st.api)
    return st


def test_ensure_brain_leaves_an_awake_brain_alone(jc, brain):
    brain.states = [True]
    jc.ensure_brain(brain.cfg)
    assert brain.urls == ["http://192.0.2.20:8080/health"] and brain.api.calls == []


def test_ensure_brain_wakes_and_waits(jc, brain, capsys):
    brain.states = [False, False, True]
    jc.ensure_brain(brain.cfg)
    assert brain.api.paths("POST") == ["/api/helios/wake"] and brain.sleeps == 2
    assert "the brain is up" in capsys.readouterr().out


@pytest.mark.parametrize(
    "wake, states, says",
    [
        ((409, {}), [False], "can't be woken"),
        (None, [False], "can't be woken"),
        ((200, {}), [False] * 9, "still not up"),
    ],
    ids=["wake-off", "orchestrator-down", "never-up"],
)
def test_ensure_brain_never_refuses(jc, brain, capsys, wake, states, says):
    brain.api.table[("POST", "/api/helios/wake")] = jc.Fatal("down") if wake is None else wake
    brain.states = states
    jc.ensure_brain(brain.cfg)  # returns: the box opens either way
    assert says in capsys.readouterr().out


def test_ensure_brain_without_a_model_url(jc, brain):
    brain.cfg.env["MODEL_URL"] = "not a url"
    jc.ensure_brain(brain.cfg)
    assert brain.urls == [] and brain.api.calls == []


def test_wait_for_orchestrator(jc, cfg, monkeypatch):
    clock = itertools.count(0, 10)
    monkeypatch.setattr(jc, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    monkeypatch.setattr(jc, "api", _Api({("GET", "/health"): (200, {})}))
    jc.wait_for_orchestrator(cfg, 60)
    monkeypatch.setattr(jc, "api", _Api({("GET", "/health"): jc.Fatal("refused")}))
    with pytest.raises(jc.Fatal, match="not healthy"):
        jc.wait_for_orchestrator(cfg, 60)


@needs_git
def test_make_bundle_caches_main_per_commit(jc, home, capsys):
    root = home.root
    _git("init", "-q", "-b", "main", cwd=root)
    (root / "x.py").write_text("x = 1\n")
    _git("add", "x.py", cwd=root)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base", cwd=root)
    base, path = jc.make_bundle(home)
    assert base == _git_out("rev-parse", "main", cwd=root) and path == home.bundles / f"{base}.bundle"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert "refs/heads/main" in _git_out("bundle", "list-heads", str(path), cwd=root)
    assert "bundling main" in capsys.readouterr().out
    before = path.stat().st_mtime_ns
    assert jc.make_bundle(home) == (base, path)
    assert path.stat().st_mtime_ns == before and "bundling" not in capsys.readouterr().out
    assert [p.name for p in home.bundles.iterdir()] == [path.name]  # no temp files left


# ----------------------------------------------------------- launcher: start


def _start_args(**over) -> argparse.Namespace:
    args = {"name": "demo", "cont": False, "ask": False, "hours": None, "task": None}
    args.update(over)
    return argparse.Namespace(**args)


class _Sink:
    def __init__(self, into: list):
        self.into = into

    def write(self, data: bytes) -> None:
        self.into.append(data)

    def close(self) -> None:
        self.into.append(None)


@pytest.fixture
def keep_signals():
    """cmd_start sets SIGHUP/SIGTERM handlers and ignores SIGINT during cleanup: put them back."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        if handler is not None:
            signal.signal(sig, handler)


@pytest.fixture
def start(jc, home, monkeypatch, keep_signals):
    """cmd_start with docker, the network check, the brain wake and the API stubbed out."""
    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=4)).isoformat(timespec="seconds")
    st = types.SimpleNamespace(cfg=home, running=[], code=0, box_output=b"", popen=[], kills=[], stdin=[])
    st.expires = expires
    st.api = _Api(
        {("POST", "/api/coder/sessions"): (201, {"id": TID, "token": "jcs_SECRET-value", "expires_at": expires})}
    )

    class Proc:
        def __init__(self, cmd, env=None, stdin=None, stdout=None, stderr=None):
            st.popen.append({"cmd": cmd, "env": env, "stderr": stderr})
            self.stdin = _Sink(st.stdin) if stdin == subprocess.PIPE else None
            self.stdout = None
            if stdout == subprocess.PIPE:
                r, w = os.pipe()
                os.write(w, st.box_output)
                os.close(w)
                self.stdout = os.fdopen(r, "rb")

        def wait(self):
            if self.stdout is not None:
                self.stdout.close()
            return st.code

    def make_bundle(cfg):
        path = cfg.bundles / f"{BASE}.bundle"
        path.write_bytes(b"bundle")
        return BASE, path

    stubs = {
        "check_image": lambda cfg: None,
        "checked_network": lambda cfg: None,
        "reap_overdue": lambda: [],
        "running_boxes": lambda kind=None: list(st.running),
        "make_bundle": make_bundle,
        "ensure_brain": lambda cfg: None,
        "selftest_env": lambda cfg: {"JESS_SELFTEST_HOSTS": "192.0.2.1"},
        "flush_terminal_input": lambda: None,
        "kill_cid": st.kills.append,
        "api": st.api,
        "subprocess": _fake_subprocess(Popen=Proc),
    }
    for name, value in stubs.items():
        monkeypatch.setattr(jc, name, value)
    return st


def test_start_token_goes_by_env_and_is_revoked(jc, start):
    assert jc.cmd_start(start.cfg, _start_args()) == 0
    (run,) = start.popen
    cmd, env = run["cmd"], run["env"]
    assert not any("jcs_" in part for part in cmd)  # never in argv (ps, docker inspect)
    assert env["JESS_BRAIN_KEY"] == "jcs_SECRET-value" and "JESS_BRAIN_KEY" in _flag_value(cmd, "-e")
    assert "-it" in cmd and cmd[-1] == "session"
    deadline = int(dt.datetime.fromisoformat(start.expires).timestamp())
    assert f"JESS_CODER_DEADLINE={deadline}" in _flag_value(cmd, "-e")
    assert f"jess-coder.expires={deadline}" in _flag_value(cmd, "--label")
    bundle = start.cfg.bundles / f"{BASE}.bundle"
    assert f"type=bind,src={bundle},dst=/bundle/repo.bundle,readonly" in _flag_value(cmd, "--mount")
    assert start.api.calls[0] == ("POST", "/api/coder/sessions", {"label": "demo"})
    assert start.api.paths("DELETE") == [f"/api/coder/sessions/{TID}"]
    assert len(start.kills) == 1  # killed by its cidfile whatever happened inside
    meta = jc.read_meta(start.cfg, "demo")
    assert meta["base"] == BASE and meta["created"] and meta["last_opened"]
    sdir = start.cfg.sessions / "demo"
    assert stat.S_IMODE(sdir.stat().st_mode) == 0o700
    assert stat.S_IMODE((sdir / "box").stat().st_mode) == 0o777


@pytest.mark.parametrize("code, rc, metric", [(0, 0, "1"), (86, 1, "0"), (124, 1, None), (137, 1, None), (2, 1, None)])
def test_start_exit_code_decides_the_metric(jc, start, capsys, code, rc, metric):
    """Only a box that got past the self-test (0) or failed it (86) says anything about isolation."""
    start.code = code
    assert jc.cmd_start(start.cfg, _start_args()) == rc
    assert _metric(start.cfg) == metric
    assert start.api.paths("DELETE") == [f"/api/coder/sessions/{TID}"]
    out = capsys.readouterr().out
    assert ("self-test FAILED" in out) is (code == 86)
    assert "reached its token's expiry" not in out  # the token is hours from expiring


def test_start_reports_expiry_only_once_the_token_ran_out(jc, start, capsys):
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
    start.api.table[("POST", "/api/coder/sessions")] = (201, {"id": TID, "token": "jcs_x", "expires_at": past})
    start.code = 124
    assert jc.cmd_start(start.cfg, _start_args()) == 1
    assert "reached its token's expiry" in capsys.readouterr().out


def test_start_headless_task_goes_on_stdin_and_output_is_made_safe(jc, start, capsys):
    start.box_output = b"working\n\x1b]52;c;ZWNobw==\x07done\n"
    assert jc.cmd_start(start.cfg, _start_args(task="fix the failing test", hours=2.0)) == 0
    run = start.popen[0]
    cmd = run["cmd"]
    assert "-i" in cmd and "-it" not in cmd and cmd[-2:] == ["session", "--headless"]
    assert run["stderr"] == subprocess.STDOUT  # the box's stderr is made terminal-safe too
    assert not any("fix the failing test" in part for part in cmd)
    assert start.stdin == [b"fix the failing test", None]
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "^[]52;c;ZWNobw==^Gdone" in out
    assert start.api.calls[0][2] == {"label": "demo", "hours": 2.0}


def test_start_resumes_without_a_new_bundle(jc, start, monkeypatch, capsys):
    _write_session(start.cfg)
    monkeypatch.setattr(jc, "make_bundle", _boom)
    assert jc.cmd_start(start.cfg, _start_args(cont=True, ask=True)) == 0
    cmd = start.popen[0]["cmd"]
    assert cmd[-3:] == ["session", "--continue", "--ask"]
    # The base bundle was pruned: the box resumes from its workspace, none is mounted.
    assert _flag_value(cmd, "--mount") == [f"type=bind,src={start.cfg.sessions / 'demo' / 'box'},dst=/work"]
    assert "resuming session demo" in capsys.readouterr().out


@pytest.mark.parametrize(
    "over, match",
    [
        ({"task": "x", "ask": True}, "--task"),
        ({"task": "   "}, "--task"),
        ({"task": ""}, "--task"),
        ({"name": "demo-patch"}, "reserved"),
        ({"name": "Demo"}, "session names"),
    ],
    ids=["task-and-ask", "task-blank", "task-empty", "reserved", "bad-name"],
)
def test_start_refuses_bad_arguments_first(jc, cfg, monkeypatch, over, match):
    monkeypatch.setattr(jc, "check_home", _boom)
    monkeypatch.setattr(jc, "api", _boom)
    with pytest.raises(jc.Fatal, match=match):
        jc.cmd_start(cfg, _start_args(**over))


def test_start_refuses_while_coder_is_off(jc, cfg, monkeypatch):
    cfg.env["CODER_ENABLED"] = "false"
    monkeypatch.setattr(jc, "check_home", _boom)
    with pytest.raises(jc.Fatal, match="CODER_ENABLED"):
        jc.cmd_start(cfg, _start_args())


@pytest.mark.parametrize(
    "running, match",
    [(["jess-coder-demo"], "already open"), (["jess-coder-a", "jess-coder-b"], "close one first")],
    ids=["same-name", "too-many"],
)
def test_start_refuses_when_sessions_are_open(jc, start, running, match):
    start.running = running
    with pytest.raises(jc.Fatal, match=match):
        jc.cmd_start(start.cfg, _start_args())
    assert start.api.calls == [] and start.popen == []
    assert not (start.cfg.sessions / "demo").exists()


def test_start_reaps_overdue_boxes_before_counting(jc, start, monkeypatch, capsys):
    order = []
    monkeypatch.setattr(jc, "reap_overdue", lambda: order.append("reap") or ["jess-coder-old"])
    monkeypatch.setattr(jc, "running_boxes", lambda kind=None: order.append("count") or ["jess-coder-other"])
    assert jc.cmd_start(start.cfg, _start_args()) == 0  # one other session: still room
    assert order[:2] == ["reap", "count"]
    assert "killed jess-coder-old" in capsys.readouterr().out


def test_start_refuses_a_session_folder_without_meta(jc, start):
    (start.cfg.sessions / "demo").mkdir()
    with pytest.raises(jc.Fatal, match="no valid meta.json"):
        jc.cmd_start(start.cfg, _start_args())
    assert start.api.calls == []


@pytest.mark.parametrize(
    "answer, match",
    [
        ((404, {}), "gate is off"),
        ((201, {"id": TID, "token": "sk-not-ours"}), "could not mint"),
        ((500, {}), "HTTP 500"),
    ],
    ids=["gate-off", "foreign-token", "error"],
)
def test_start_mint_failure_opens_no_box_and_revokes_by_label(jc, start, answer, match):
    start.api.table[("POST", "/api/coder/sessions")] = answer
    listing = [{"id": TID, "label": "demo"}, {"id": "f" * 32, "label": "other"}]
    start.api.table[("GET", "/api/coder/sessions")] = (200, {"sessions": listing})
    with pytest.raises(jc.Fatal, match=match):
        jc.cmd_start(start.cfg, _start_args())
    assert start.popen == []
    assert start.api.paths("DELETE") == [f"/api/coder/sessions/{TID}"]


def test_start_odd_mint_answer_still_revokes(jc, start):
    start.api.table[("POST", "/api/coder/sessions")] = (201, {"id": TID, "token": "jcs_x", "expires_at": "soon"})
    with pytest.raises(jc.Fatal, match="malformed token expiry"):
        jc.cmd_start(start.cfg, _start_args())
    assert start.popen == []
    assert start.api.paths("DELETE") == [f"/api/coder/sessions/{TID}"]


def test_start_warns_when_the_token_cannot_be_revoked(jc, start, capsys):
    start.api.table[("DELETE", f"/api/coder/sessions/{TID}")] = jc.Fatal("orchestrator down")
    assert jc.cmd_start(start.cfg, _start_args()) == 0
    assert "token not revoked" in capsys.readouterr().out


# ------------------------------------------------- launcher: patch / rm / test


@pytest.fixture
def boxes(jc, home, monkeypatch):
    """Helper boxes stubbed (``result`` is what the box returns); host git in the patch check runs for real."""
    st = types.SimpleNamespace(cfg=home, result=(0, b"", b"", ""), runs=[], running=[])
    real = jc.run_captured

    def run_captured(cmd, cidfile, *, limit, timeout, **kw):
        if cidfile is None:  # check_box_diff's own git, not a box
            return real(cmd, cidfile, limit=limit, timeout=timeout, **kw)
        st.runs.append({"cmd": cmd, "limit": limit, "timeout": timeout})
        return st.result

    stubs = {
        "run_captured": run_captured,
        "check_image": lambda cfg: None,
        "checked_network": lambda cfg: None,
        "reap_overdue": lambda: [],
        "selftest_env": lambda cfg: {"JESS_SELFTEST_HOSTS": "192.0.2.1"},
        "running_boxes": lambda kind=None: list(st.running),
    }
    for name, value in stubs.items():
        monkeypatch.setattr(jc, name, value)
    return st


def _patch_args(allow=(), show=False) -> argparse.Namespace:
    return argparse.Namespace(name="demo", allow=list(allow), show=show)


def test_patch_preconditions(jc, boxes):
    with pytest.raises(jc.Fatal, match="no session demo"):
        jc.cmd_patch(boxes.cfg, _patch_args())
    _write_session(boxes.cfg)
    boxes.running = ["jess-coder-demo"]
    with pytest.raises(jc.Fatal, match="is open"):
        jc.cmd_patch(boxes.cfg, _patch_args())
    boxes.running = []
    with pytest.raises(jc.Fatal, match="bundle"):
        jc.cmd_patch(boxes.cfg, _patch_args())
    assert boxes.runs == []


def test_patch_box_is_offline_and_read_only(jc, boxes, capsys):
    _write_session(boxes.cfg)
    (boxes.cfg.bundles / f"{BASE}.bundle").write_bytes(b"x")
    assert jc.cmd_patch(boxes.cfg, _patch_args()) == 0
    (run,) = boxes.runs
    cmd = run["cmd"]
    assert _flag_value(cmd, "--network") == ["none"]
    assert _flag_value(cmd, "--mount") == [f"type=bind,src={boxes.cfg.sessions / 'demo' / 'box'},dst=/work,readonly"]
    assert _flag_value(cmd, "--name") == ["jess-coder-demo-patch"] and cmd[-2:] == ["patch", BASE]
    assert "-it" not in cmd and "-i" not in cmd
    assert (run["limit"], run["timeout"]) == (jc.MAX_PATCH_BYTES, jc.PATCH_TIMEOUT_S)
    assert "no changes" in capsys.readouterr().out


@pytest.mark.parametrize(
    "result, match",
    [((0, b"x", b"", "too_large"), "over 8 MB"), ((-9, b"", b"", "timeout"), "took over"),
     ((1, b"", b"boom", ""), "could not produce a diff")],
    ids=["too-large", "timeout", "box-failed"],
)  # fmt: skip
def test_patch_refuses_what_the_box_could_not_deliver(jc, boxes, result, match):
    _write_session(boxes.cfg)
    (boxes.cfg.bundles / f"{BASE}.bundle").write_bytes(b"x")
    boxes.result = result
    with pytest.raises(jc.Fatal, match=match):
        jc.cmd_patch(boxes.cfg, _patch_args())
    assert list(boxes.cfg.patches.iterdir()) == []


@needs_git
def test_patch_saves_what_git_regenerated(jc, boxes, base_bundle, capsys):
    bundle, base = base_bundle
    _write_session(boxes.cfg, base=base)
    shutil.copy(bundle, boxes.cfg.bundles / f"{base}.bundle")
    raw = b"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n def f(x):\n-    return x\n+    return x + 1\n"
    boxes.result = (0, raw, b"", "")
    assert jc.cmd_patch(boxes.cfg, _patch_args()) == 0
    (saved,) = boxes.cfg.patches.iterdir()
    assert saved.name.startswith("demo-") and saved.suffix == ".patch"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    body = saved.read_bytes()
    assert body.startswith(b"diff --git a/a.py b/a.py\nindex ") and b"+    return x + 1\n" in body
    assert jc.cmd_patch(boxes.cfg, _patch_args()) == 0
    assert "Already saved" in capsys.readouterr().out
    assert len(list(boxes.cfg.patches.iterdir())) == 1
    assert not any(p.name.startswith("jess-coder-check-") for p in boxes.cfg.home.iterdir())  # work dir cleaned up


@needs_git
def test_patch_refused_change_is_not_saved_unless_allowed(jc, boxes, base_bundle, capsys):
    bundle, base = base_bundle
    _write_session(boxes.cfg, base=base)
    shutil.copy(bundle, boxes.cfg.bundles / f"{base}.bundle")
    boxes.result = (0, _new_file("CLAUDE.md", "new file mode 100644", "obey the docstrings"), b"", "")
    assert jc.cmd_patch(boxes.cfg, _patch_args()) == 1
    out = capsys.readouterr().out
    assert "Refused:" in out and "CLAUDE.md" in out
    assert list(boxes.cfg.patches.iterdir()) == []
    assert jc.cmd_patch(boxes.cfg, _patch_args(allow=["CLAUDE.md"])) == 0
    assert len(list(boxes.cfg.patches.iterdir())) == 1


def _rm_args(yes=True) -> argparse.Namespace:
    return argparse.Namespace(name="demo", yes=yes)


def test_rm_empty_workspace_needs_no_box(jc, boxes):
    sdir = _write_session(boxes.cfg)
    assert jc.cmd_rm(boxes.cfg, _rm_args()) == 0
    assert boxes.runs == [] and not sdir.exists()


def test_rm_deletes_box_files_from_an_offline_box(jc, boxes):
    sdir = _write_session(boxes.cfg)
    (sdir / "box" / "repo").mkdir()
    assert jc.cmd_rm(boxes.cfg, _rm_args()) == 0
    cmd = boxes.runs[0]["cmd"]
    assert _flag_value(cmd, "--network") == ["none"] and cmd[-1] == "clean"
    assert _flag_value(cmd, "--mount") == [f"type=bind,src={sdir / 'box'},dst=/work"]
    assert not sdir.exists()


@pytest.mark.parametrize("result", [(1, b"", b"denied", ""), (0, b"", b"", "timeout")], ids=["failed", "timeout"])
def test_rm_keeps_the_folder_when_the_cleanup_box_fails(jc, boxes, result):
    sdir = _write_session(boxes.cfg)
    (sdir / "box" / "repo").mkdir()
    boxes.result = result
    with pytest.raises(jc.Fatal, match="cleanup box failed"):
        jc.cmd_rm(boxes.cfg, _rm_args())
    assert (sdir / "meta.json").is_file()


def test_rm_asks_and_refuses_open_sessions(jc, boxes, monkeypatch):
    sdir = _write_session(boxes.cfg)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert jc.cmd_rm(boxes.cfg, _rm_args(yes=False)) == 1
    boxes.running = ["jess-coder-demo"]
    with pytest.raises(jc.Fatal, match="is open"):
        jc.cmd_rm(boxes.cfg, _rm_args())
    assert sdir.is_dir()


@pytest.mark.parametrize(
    "result, rc, metric",
    [((0, b"ok   a\nselftest: PASS\n", b"", ""), 0, "1"), ((1, b"FAIL a\x1b[2J\n", b"", ""), 1, "0"),
     ((0, b"ok   a\n", b"", "timeout"), 1, "0"), ((-9, b"x" * 10, b"", "too_large"), 1, "0")],
    ids=["pass", "fail", "timeout", "too-large"],
)  # fmt: skip
def test_selftest_command_records_the_outcome(jc, boxes, capsys, result, rc, metric):
    boxes.result = result
    assert jc.cmd_selftest(boxes.cfg, argparse.Namespace(wait_health=0)) == rc
    assert _metric(boxes.cfg) == metric
    cmd = boxes.runs[0]["cmd"]
    assert cmd[-1] == "selftest" and _flag_value(cmd, "--network") == [boxes.cfg.network]
    assert "--mount" not in cmd and "-i" not in cmd and "-it" not in cmd
    assert "\x1b" not in capsys.readouterr().out


def test_build_if_stale_skips_a_current_image(jc, cfg, monkeypatch, capsys):
    monkeypatch.setattr(jc, "image_inputs", lambda c: "abc123")
    monkeypatch.setattr(jc, "image_label", lambda c: "abc123")
    monkeypatch.setattr(jc, "subprocess", _fake_subprocess(run=_boom))
    assert jc.cmd_build(cfg, argparse.Namespace(if_stale=True)) == 0
    assert "up to date" in capsys.readouterr().out


@pytest.mark.parametrize(
    "if_stale, label, build_rc",
    [(True, "old", 0), (False, "abc123", 0), (True, None, 1)],
    ids=["stale", "forced", "build-fails"],
)
def test_build_labels_the_image_and_prunes_after_success(jc, cfg, monkeypatch, if_stale, label, build_rc):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return _completed(returncode=build_rc if len(calls) == 1 else 0)

    monkeypatch.setattr(jc, "image_inputs", lambda c: "abc123")
    monkeypatch.setattr(jc, "image_label", lambda c: label)
    monkeypatch.setattr(jc, "subprocess", _fake_subprocess(run=fake_run))
    assert jc.cmd_build(cfg, argparse.Namespace(if_stale=if_stale)) == build_rc
    (build_cmd, kw), *rest = calls
    assert build_cmd == ["docker", "compose", "--profile", "coder", "build", "coder"]
    assert kw["cwd"] == cfg.root and kw["env"]["JESS_CODER_INPUTS"] == "abc123"
    if build_rc == 0:
        assert [c for c, _ in rest] == [["docker", "image", "prune", "-f", "--filter", "label=jess.coder=1"]]
    else:
        assert rest == []


# --------------------------------------------------------------- launcher: CLI


def test_bare_name_means_start(jc):
    args = jc.parse(["demo", "--task", "x", "--hours", "1.5"])
    assert (args.cmd, args.name, args.task, args.hours) == ("start", "demo", "x", 1.5)
    args = jc.parse(["patch", "demo", "--allow", "CLAUDE.md", "--allow", ".github/"])
    assert args.allow == ["CLAUDE.md", ".github/"] and args.show is False


def test_main_reports_refusals_terminal_safe(jc, tmp_path, monkeypatch, capsys):
    root = tmp_path / "x\x1b[2Jy"
    root.mkdir()
    monkeypatch.setenv("JESS_GATEWAY_ROOT", str(root))
    assert jc.main(["ls"]) == 2
    err = capsys.readouterr().err
    assert "no .env in" in err and "\x1b" not in err and "^[[2J" in err


def test_main_exit_codes(jc, cfg, monkeypatch, capsys):
    assert jc.main(["enabled"]) == 0

    def fails(cfg, args):
        raise subprocess.CalledProcessError(1, ["docker", "ps", "--secret-arg"])

    monkeypatch.setattr(jc, "cmd_ls", fails)
    assert jc.main(["ls"]) == 2
    err = capsys.readouterr().err
    assert "docker failed (exit 1)" in err and "--secret-arg" not in err


def test_allow_suggestion_is_shell_quoted_and_case_exact(jc):
    path = "frontend/src/app/(private)/.env"
    problems = jc.review_changes([_entry(path, status="A", old_mode="000000")], [], "", [])
    assert any(f"--allow '{path}'" in p for p in problems)
    assert jc.review_changes([_entry(".GitHub/workflows/x.yml")], [], "", [".github/"])  # --allow is case-exact


@needs_git
@pytest.mark.parametrize("path", ["orchestrator/.git/hooks/post-checkout", "x/.GIT/config", ".git/hooks/pre-commit"])
def test_git_dirs_anywhere_never_get_through(jc, base_bundle, path):
    bundle, base = base_bundle
    try:
        _e, _s, _t, _c, problems = jc.check_box_diff(bundle, base, _new_file(path, "new file mode 100644", "x"), [])
    except jc.Fatal:
        return  # git refused to apply it
    assert problems


# -------------------------------------------------- patch: more character cases


@pytest.mark.parametrize(
    "line, what",
    [
        ("+++ b/a.py\x1b[2J", "control character 0x1b"),
        ("--- a/a.py\x1b[2J", "control character 0x1b"),
        ("+x = 1\ry = 2", "control character 0x0d"),
        ("+x = 1\r\r", "control character 0x0d"),
        ("+x = 1\x7f", "control character 0x7f"),
        ("+x = '\x85'", "control character 0x85"),
        ("+x = '\ufeff'", "invisible character U+FEFF"),
        ("+x = 'a\u00adb'", "invisible character U+00AD"),
        ("\\ No newline\x1b", "control character 0x1b"),
    ],
    ids=["new-header", "old-header", "lone-cr", "double-cr", "del", "c1", "bom", "soft-hyphen", "no-newline-line"],
)
def test_more_characters_a_reviewer_could_miss(jc, line, what):
    problems = jc.text_problems(line + "\n")
    assert problems == [f"line 1: {what}"]


def test_crlf_line_ends_pass(jc):
    assert jc.text_problems("+x = 1\r\n+y = 2\r\n") == []


def test_text_problems_one_per_line_five_at_most(jc):
    assert jc.text_problems("+ok\n+\x1b\x07\u202e\n") == ["line 2: control character 0x1b"]
    many = jc.text_problems("".join(f"+x{i}\x1b\n" for i in range(9)))
    assert many == [f"line {i}: control character 0x1b" for i in range(1, 6)]


# ==================================================================== entrypoint


def _exit_code(fn, *args) -> int:
    with pytest.raises(SystemExit) as exc:
        fn(*args)
    return exc.value.code


def _locked_tree(root: Path, outside: Path) -> None:
    """A tree the box could leave behind: 000 and 500 dirs, links out of the tree."""
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f").write_text("x")
    (root / "ro").mkdir()
    (root / "ro" / "g").write_text("y")
    (root / "out").symlink_to(outside, target_is_directory=True)
    (root / "a" / "out2").symlink_to(outside)
    os.chmod(root / "a" / "b", 0o000)
    os.chmod(root / "a", 0o000)
    os.chmod(root / "ro", 0o500)


@pytest.fixture
def outside(tmp_path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "keep.txt").write_text("keep")
    os.chmod(path, 0o755)
    return path


def _untouched(path: Path) -> bool:
    return (path / "keep.txt").read_text() == "keep" and stat.S_IMODE(path.stat().st_mode) == 0o755


def test_rmtree_opens_locked_dirs_and_never_follows_links(entry, tmp_path, outside):
    tree = tmp_path / "tree"
    _locked_tree(tree, outside)
    entry._rmtree(tree)
    assert not os.path.lexists(tree)
    assert _untouched(outside)  # not emptied, and not chmod-ed through the link


def test_clean_empties_the_workspace_only(entry, tmp_path, outside, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    _locked_tree(work / "repo", outside)
    (work / ".qwen-state").mkdir()
    (work / "file").write_text("x")
    (work / "dirlink").symlink_to(outside, target_is_directory=True)
    (work / "filelink").symlink_to(outside / "keep.txt")
    monkeypatch.setattr(entry, "WORK", work)
    entry.clean([])
    assert work.is_dir() and list(work.iterdir()) == []
    assert _untouched(outside)
    assert _exit_code(entry.clean, ["x"]) == 2


def test_rmtree_given_a_link_leaves_its_target_alone(entry, tmp_path, outside):
    """A box can leave /work/repo.partial as a link; the next start's cleanup must not go through it."""
    link = tmp_path / "repo.partial"
    link.symlink_to(outside, target_is_directory=True)
    with contextlib.suppress(OSError):
        entry._rmtree(link)
    assert _untouched(outside)


@pytest.fixture
def workspace(entry, tmp_path, monkeypatch, base_bundle):
    bundle, base = base_bundle
    work = tmp_path / "work"
    work.mkdir()
    ws = types.SimpleNamespace(work=work, repo=work / "repo", partial=work / "repo.partial", base=base)
    for name, value in {"WORK": work, "REPO": ws.repo, "PARTIAL": ws.partial, "BUNDLE": bundle}.items():
        monkeypatch.setattr(entry, name, value)
    return ws


@needs_git
def test_prepare_workspace_unpacks_the_bundle_on_its_branch(entry, workspace):
    entry.prepare_workspace("demo", workspace.base)
    repo = workspace.repo
    assert _git_out("rev-parse", "HEAD", cwd=repo) == workspace.base
    assert _git_out("symbolic-ref", "--short", "HEAD", cwd=repo) == "coder/demo"
    assert (repo / "a.py").read_text() == "def f(x):\n    return x\n"
    assert _git_out("config", "user.email", cwd=repo) == "coder-box@jess.invalid"
    assert not workspace.partial.exists()


@needs_git
def test_prepare_workspace_resumes_an_existing_one(entry, workspace, capsys):
    entry.prepare_workspace("demo", workspace.base)
    (workspace.repo / "a.py").write_text("changed\n")
    entry.prepare_workspace("demo", "f" * 40)  # the base only matters on a first start
    assert (workspace.repo / "a.py").read_text() == "changed\n"
    assert "resuming" in capsys.readouterr().out


@needs_git
def test_prepare_workspace_replaces_an_interrupted_first_start(entry, workspace):
    junk = workspace.partial / "locked"
    junk.mkdir(parents=True)
    (junk / "f").write_text("x")
    os.chmod(junk, 0o000)
    entry.prepare_workspace("demo", workspace.base)
    assert not workspace.partial.exists() and (workspace.repo / "a.py").is_file()


@needs_git
def test_prepare_workspace_never_shows_a_half_made_repo(entry, workspace):
    with pytest.raises(subprocess.CalledProcessError):
        entry.prepare_workspace("demo", "f" * 40)  # not in the bundle
    assert not workspace.repo.exists()


def test_prepare_workspace_refusals(entry, tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "repo").mkdir(parents=True)
    paths = {"WORK": work, "REPO": work / "repo", "PARTIAL": work / "repo.partial", "BUNDLE": tmp_path / "none"}
    for name, value in paths.items():
        monkeypatch.setattr(entry, name, value)
    assert _exit_code(entry.prepare_workspace, "demo", BASE) == 2  # a repo dir that isn't a workspace
    (work / "repo").rmdir()
    assert _exit_code(entry.prepare_workspace, "demo", BASE) == 2  # no bundle mounted
    assert list(work.iterdir()) == []


def test_prepare_home_links_chat_history_into_the_workspace(entry, tmp_path, monkeypatch):
    home, state = tmp_path / "home", tmp_path / "work" / ".qwen-state"
    monkeypatch.setattr(entry, "HOME", home)
    monkeypatch.setattr(entry, "STATE", state)
    entry.prepare_home()
    entry.prepare_home()  # a resumed box runs it again
    link = home / ".qwen" / "projects"
    assert link.is_symlink() and link.resolve() == (state / "projects").resolve()
    assert stat.S_IMODE((home / ".qwen").stat().st_mode) == 0o700


@pytest.fixture
def sess(entry, tmp_path, monkeypatch):
    """session() with the self-test, workspace steps and exec stubbed out."""
    st = types.SimpleNamespace(selftests=0, selftest_code=0, prepared=None, argv=None)

    class Exec(Exception):
        pass

    def fake_selftest():
        st.selftests += 1
        return st.selftest_code

    def fake_exec(file, argv):
        st.argv = argv
        raise Exec

    st.Exec = Exec
    for key, value in {
        "JESS_CODER_NAME": "demo",
        "JESS_CODER_BASE": BASE,
        "JESS_BRAIN_KEY": "jcs_SECRET-value",
        "JESS_CODER_DEADLINE": str(int(time.time() + 3600)),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("QWEN_CODE_UNATTENDED_RETRY", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("PROMPT")
    stubs = {
        "run_selftest": fake_selftest,
        "prepare_workspace": lambda name, base: setattr(st, "prepared", (name, base)),
        "prepare_home": lambda: None,
        "REPO": repo,
        "SYSTEM_PROMPT": prompt,
    }
    for name, value in stubs.items():
        monkeypatch.setattr(entry, name, value)
    monkeypatch.setattr(entry.os, "execvp", fake_exec)
    monkeypatch.chdir(tmp_path)
    return st


@pytest.mark.parametrize(
    "key, value",
    [
        ("JESS_CODER_NAME", None),
        ("JESS_CODER_NAME", "Demo"),
        ("JESS_CODER_NAME", "demo\n"),
        ("JESS_CODER_BASE", "abc"),
        ("JESS_CODER_BASE", "A" * 40),
        ("JESS_BRAIN_KEY", None),
        ("JESS_BRAIN_KEY", ""),
        ("JESS_CODER_DEADLINE", None),
        ("JESS_CODER_DEADLINE", "soon"),
    ],
    ids=["no-name", "upper-name", "newline-name", "short-base", "upper-base", "no-key", "empty-key", "no-deadline",
         "bad-deadline"],
)  # fmt: skip
def test_session_refuses_bad_env_before_the_selftest(entry, sess, monkeypatch, key, value):
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    assert _exit_code(entry.session, []) == 2
    assert sess.selftests == 0 and sess.argv is None


@pytest.mark.parametrize("args", [["--yolo"], ["--continue", "--approval-mode=yolo"]], ids=["yolo", "smuggled"])
def test_session_refuses_unknown_options(entry, sess, args):
    assert _exit_code(entry.session, args) == 2
    assert sess.selftests == 0 and sess.argv is None


def test_session_stays_closed_when_the_selftest_fails(entry, sess):
    sess.selftest_code = 1
    assert _exit_code(entry.session, []) == entry.SELFTEST_FAILED
    assert sess.prepared is None and sess.argv is None


def test_session_refuses_a_token_about_to_expire(entry, sess, monkeypatch):
    monkeypatch.setenv("JESS_CODER_DEADLINE", str(int(time.time() + 30)))
    assert _exit_code(entry.session, []) == 2
    assert sess.argv is None


@pytest.mark.parametrize(
    "args, approval, extra",
    [
        ([], "yolo", []),
        (["--ask"], "default", []),
        (["--continue"], "yolo", ["--continue"]),
        (["--headless"], "yolo", ["--output-format", "text", "--max-wall-time", "45m"]),
    ],
    ids=["auto", "ask", "continue", "headless"],
)
def test_session_execs_qwen_under_a_deadline(entry, sess, args, approval, extra):
    with pytest.raises(sess.Exec):
        entry.session(args)
    argv = sess.argv
    assert argv[:3] == ["timeout", "--foreground", "--kill-after=30"]
    assert 3500 < int(argv[3]) <= 3600 and argv[4] == "qwen"
    assert argv[5:9] == ["--approval-mode", approval, "--exclude-tools", entry.EXCLUDE_TOOLS]
    assert argv[9:11] == ["--system-prompt", "PROMPT"] and argv[11:] == extra
    assert not any("jcs_" in part for part in argv)
    assert sess.prepared == ("demo", BASE) and Path.cwd() == entry.REPO.resolve()
    assert (os.environ.get("QWEN_CODE_UNATTENDED_RETRY") == "1") is ("--headless" in args)


@pytest.mark.parametrize("args", [[], ["abc"], ["A" * 40], ["a" * 40, "x"]], ids=["none", "short", "upper", "extra"])
def test_patch_mode_wants_exactly_one_base_sha(entry, args):
    assert _exit_code(entry.patch, args) == 2


def test_patch_mode_without_a_workspace(entry, tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "REPO", tmp_path / "repo")
    assert _exit_code(entry.patch, [BASE]) == 2


@pytest.mark.parametrize("argv", [["shell"], ["selftest", "x"], ["clean", "x"]], ids=["unknown", "selftest", "clean"])
def test_entrypoint_refuses_unknown_modes(entry, monkeypatch, argv):
    monkeypatch.setattr(entry, "run_selftest", _boom)
    monkeypatch.setattr(entry, "WORK", Path("/nonexistent-work"))
    assert _exit_code(entry.main, argv) == 2


def test_entrypoint_defaults_to_the_selftest(entry, monkeypatch):
    monkeypatch.setattr(entry, "run_selftest", lambda: 86)
    assert _exit_code(entry.main, []) == 86


# ====================================================================== selftest


def _fake_files(monkeypatch, module, files: dict) -> None:
    """Serve ``open(path)`` from ``files``; anything else is FileNotFoundError."""

    def fake_open(path, *a, **k):
        if path in files:
            return io.StringIO(files[path])
        raise FileNotFoundError(path)

    monkeypatch.setattr(module, "open", fake_open, raising=False)


@pytest.fixture
def fake_socket(selftest, monkeypatch):
    st = types.SimpleNamespace(exc=None, calls=[])

    class Sock:
        def __init__(self, family, kind):
            self.family = family
            self.timeout = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            self.timeout = t

        def connect(self, addr):
            st.calls.append((self.family, addr, self.timeout))
            if st.exc is not None:
                raise st.exc

    fake = types.SimpleNamespace(
        socket=Sock,
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        SOCK_STREAM=socket.SOCK_STREAM,
        if_nametoindex=lambda name: {"eth0": 7}[name],
    )
    monkeypatch.setattr(selftest, "socket", fake)
    return st


def _oserr(code: int) -> OSError:
    return OSError(code, os.strerror(code))


_REACH_CASES = [
    # (what connect() does, expect, hit or None)
    ("timeout", "absent", None),
    ("timeout", "closed", "timed out"),
    ("timeout", "no_route", "timed out"),
    (errno.ECONNREFUSED, "closed", None),
    (errno.ECONNREFUSED, "no_route", "ECONNREFUSED"),
    (errno.ECONNREFUSED, "absent", "ECONNREFUSED"),
    (errno.ENETUNREACH, "no_route", None),
    (errno.ENETUNREACH, "closed", None),
    (errno.EADDRNOTAVAIL, "absent", None),
    (errno.EAFNOSUPPORT, "no_route", None),
    (errno.EHOSTUNREACH, "absent", None),  # ARP for an empty in-subnet address failed
    (errno.EHOSTUNREACH, "no_route", "EHOSTUNREACH"),  # an ICMP answer: packets left the box
    (errno.EHOSTUNREACH, "closed", "EHOSTUNREACH"),
    (errno.EACCES, "no_route", "EACCES"),
    (errno.ECONNRESET, "closed", "ECONNRESET"),
    ("connect", "closed", "connected"),
    ("connect", "absent", "connected"),
]


@pytest.mark.parametrize(
    "what, expect, hit",
    _REACH_CASES,
    ids=[f"{w if isinstance(w, str) else errno.errorcode[w]}-{e}" for w, e, _ in _REACH_CASES],
)
def test_selftest_tcp_reach_expectations(selftest, fake_socket, what, expect, hit):
    fake_socket.exc = (
        {"timeout": TimeoutError("timed out"), "connect": None}.get(what) if isinstance(what, str) else _oserr(what)
    )
    got = selftest._tcp_reach(("192.0.2.1", 22, socket.AF_INET, expect))
    if hit is None:
        assert got is None
    else:
        assert got is not None and got.startswith("192.0.2.1:22 ") and hit in got
    assert fake_socket.calls == [(socket.AF_INET, ("192.0.2.1", 22), selftest.CONNECT_TIMEOUT_S)]


def test_selftest_tcp_reach_ipv6_and_link_local(selftest, fake_socket):
    fake_socket.exc = _oserr(errno.ECONNREFUSED)
    assert selftest._tcp_reach(("2001:db8::1", 443, socket.AF_INET6, "no_route")) == "[2001:db8::1]:443 ECONNREFUSED"
    selftest._tcp_reach(("fe80::1%eth0", 22, socket.AF_INET6, "no_route"))
    assert fake_socket.calls[-1][:2] == (socket.AF_INET6, ("fe80::1", 22, 0, 7))  # numeric scope
    assert selftest._tcp_connects(("192.0.2.1", 22, socket.AF_INET)) == "192.0.2.1:22 ECONNREFUSED"


@pytest.fixture
def ident(selftest, monkeypatch):
    """check_identity's inputs, box-like unless a test changes them."""
    zero = "0" * 16
    st = types.SimpleNamespace(uid=61001, euid=61001, gid=61001, groups=[61001])
    st.status = {"CapInh": zero, "CapPrm": zero, "CapEff": zero, "CapBnd": zero, "CapAmb": zero, "NoNewPrivs": "1",
                 "Seccomp": "2"}  # fmt: skip
    fake_os = types.SimpleNamespace(
        environ=os.environ,
        getuid=lambda: st.uid,
        geteuid=lambda: st.euid,
        getgid=lambda: st.gid,
        getgroups=lambda: st.groups,
    )
    monkeypatch.setattr(selftest, "os", fake_os)
    monkeypatch.setattr(selftest, "_status_fields", lambda: dict(st.status))
    monkeypatch.delenv("JESS_CODER_UID", raising=False)
    return st


def _identity(selftest):
    res = selftest.Results()
    selftest.check_identity(res)
    return res


@pytest.mark.parametrize(
    "field, value, failed",
    [
        ("uid", 1000, ["runs as the box uid"]),
        ("euid", 0, ["runs as the box uid"]),
        ("gid", 0, ["not root"]),
        ("groups", [61001, 0], ["not root"]),
        ("CapBnd", "00000000a80425fb", ["no capabilities"]),
        ("CapAmb", None, ["no capabilities"]),  # unreadable counts as a capability
        ("NoNewPrivs", "0", ["no_new_privs set"]),
        ("Seccomp", "0", ["seccomp filter on"]),
        ("Seccomp", None, ["seccomp filter on"]),
    ],
    ids=[
        "uid",
        "euid-root",
        "gid-root",
        "root-group",
        "caps",
        "caps-missing",
        "nnp-off",
        "seccomp-off",
        "seccomp-missing",
    ],
)
def test_selftest_identity_checks(selftest, ident, field, value, failed):
    assert _identity(selftest).failed == []
    if field in ident.status:
        if value is None:
            del ident.status[field]
        else:
            ident.status[field] = value
    else:
        setattr(ident, field, value)
    assert _identity(selftest).failed == failed


def test_selftest_identity_uid_comes_from_the_launcher(selftest, ident, monkeypatch):
    monkeypatch.setenv("JESS_CODER_UID", "1234")
    assert _identity(selftest).failed == ["runs as the box uid"]
    ident.uid = ident.euid = 1234
    assert _identity(selftest).failed == []


def test_selftest_status_fields_parse(selftest, monkeypatch):
    _fake_files(
        monkeypatch, selftest, {"/proc/self/status": "Name:\tpython3\nCapEff:\t0000000000000000\nSeccomp:\t2\n"}
    )
    assert selftest._status_fields() == {"Name": "python3", "CapEff": "0000000000000000", "Seccomp": "2"}


def test_selftest_results_count_and_report(selftest, capsys):
    res = selftest.Results()
    assert res.check("a", True) is True
    assert res.check("b", False, "why") is False
    assert res.check("c", False) is False
    assert res.count == 3 and res.failed == ["b", "c"]
    assert capsys.readouterr().out.splitlines() == ["ok   a", "FAIL b: why", "FAIL c"]


_MOUNTINFO = (
    "22 1 0:21 / / ro,relatime master:1 - overlay overlay ro,lowerdir=x\n"
    "23 22 0:22 / /proc rw,nosuid,nodev,noexec - proc proc rw\n"
    "24 22 0:23 / /dev rw,nosuid - tmpfs tmpfs rw\n"
    "25 22 0:24 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
    "26 22 0:25 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw,size=2g\n"
    "27 22 0:26 / /home/coder rw,nosuid,nodev - tmpfs tmpfs rw\n"
    "28 22 8:1 /srv/x /work rw,relatime shared:5 - ext4 /dev/loop0 rw\n"
    "29 22 8:1 /hosts /etc/hosts ro,relatime - ext4 /dev/sda1 rw\n"
)
_BAD_MOUNTS = (
    "30 22 8:1 /x /with\\040space rw,relatime - ext4 /dev/sda1 rw\n"
    "31 22 8:1 /y /procfs-not rw - ext4 /dev/sda1 rw\n"
    "32 22 8:1 /z /work/inner rw - ext4 /dev/sda1 rw\n"
)


def _filesystem(selftest, monkeypatch, mountinfo: str, sockets=()):
    _fake_files(monkeypatch, selftest, {"/proc/self/mountinfo": mountinfo})
    fake_os = types.SimpleNamespace(environ=os.environ, path=types.SimpleNamespace(exists=lambda p: p in sockets))
    monkeypatch.setattr(selftest, "os", fake_os)
    res = selftest.Results()
    selftest.check_filesystem(res)
    return res


def test_selftest_mounts_parse_mountinfo(selftest, monkeypatch):
    _fake_files(monkeypatch, selftest, {"/proc/self/mountinfo": _MOUNTINFO + _BAD_MOUNTS})
    mounts = selftest._mounts()
    assert mounts[0] == ("/", "ro,relatime", "overlay")
    assert ("/work", "rw,relatime", "ext4") in mounts  # optional fields don't shift the columns
    assert ("/with space", "rw,relatime", "ext4") in mounts  # octal escapes decoded


def test_selftest_filesystem_passes_a_box_like_layout(selftest, monkeypatch):
    monkeypatch.delenv("JESS_SELFTEST_EXPECT_RW", raising=False)
    res = _filesystem(selftest, monkeypatch, _MOUNTINFO)
    assert res.failed == [] and res.count == 3


def test_selftest_filesystem_flags_unexpected_writable_mounts(selftest, monkeypatch, capsys):
    monkeypatch.delenv("JESS_SELFTEST_EXPECT_RW", raising=False)
    res = _filesystem(selftest, monkeypatch, _MOUNTINFO + _BAD_MOUNTS)
    assert res.failed == ["writable only where expected"]
    assert "/procfs-not, /with space, /work/inner" in capsys.readouterr().out


def test_selftest_filesystem_flags_rw_root_and_sockets(selftest, monkeypatch):
    monkeypatch.delenv("JESS_SELFTEST_EXPECT_RW", raising=False)
    rw_root = _MOUNTINFO.replace("/ / ro,relatime", "/ / rw,relatime")
    res = _filesystem(selftest, monkeypatch, rw_root, sockets={"/run/docker.sock"})
    assert res.failed == ["root filesystem read-only", "no Docker or containerd socket"]
    no_root = "".join(line + "\n" for line in _MOUNTINFO.splitlines()[1:])
    assert "root filesystem read-only" in _filesystem(selftest, monkeypatch, no_root).failed


def test_selftest_expect_rw_narrows_the_writable_set(selftest, monkeypatch, capsys):
    monkeypatch.setenv("JESS_SELFTEST_EXPECT_RW", "/work")
    res = _filesystem(selftest, monkeypatch, _MOUNTINFO)
    assert res.failed == ["writable only where expected"]
    assert "/home/coder, /tmp" in capsys.readouterr().out


def test_selftest_proc_net_parsers(selftest, monkeypatch):
    route_head = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    subnet_route = "eth0\t0000D50A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
    files = {
        "/proc/net/dev": "Inter-|   Receive\n face |bytes    packets\n    lo: 1 2 3\n  eth0: 4 5 6\n",
        "/proc/net/route": route_head + subnet_route,
        "/proc/net/if_inet6": (
            "00000000000000000000000000000001 01 80 10 80       lo\n"
            "fe800000000000000000000000000001 02 40 20 80     eth0\n"
        ),
    }
    _fake_files(monkeypatch, selftest, files)
    assert selftest._interfaces() == ["lo", "eth0"]
    assert selftest._has_default_route_v4() is False
    assert selftest._v6_addresses() == [ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("fe80::1")]
    files["/proc/net/route"] += "eth0\t00000000\t01D5D50A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    assert selftest._has_default_route_v4() is True
    del files["/proc/net/if_inet6"]
    assert selftest._v6_addresses() == []  # IPv6 off in this netns


@pytest.fixture
def netns(selftest, monkeypatch):
    """check_network_config's inputs, box-like unless a test changes them."""
    st = types.SimpleNamespace(ifaces=["lo", "eth0"], default=False, v6=["::1"], own="10.213.250.130", disable="1")
    stubs = {
        "_interfaces": lambda: st.ifaces,
        "_has_default_route_v4": lambda: st.default,
        "_v6_addresses": lambda: [ipaddress.IPv6Address(a) for a in st.v6],
        "_own_ipv4": lambda towards: st.own,
    }
    for name, value in stubs.items():
        monkeypatch.setattr(selftest, name, value)

    def fake_open(path, *a, **k):
        if path == "/proc/sys/net/ipv6/conf/eth0/disable_ipv6" and st.disable is not None:
            return io.StringIO(st.disable + "\n")
        raise FileNotFoundError(path)

    monkeypatch.setattr(selftest, "open", fake_open, raising=False)
    monkeypatch.setenv("JESS_CODER_BOX_RANGE", "10.213.250.128/25")
    return st


def _network_config(selftest):
    res = selftest.Results()
    selftest.check_network_config(res, "10.213.250.2")
    return res


def test_selftest_network_config_passes_a_box_netns(selftest, netns):
    res = _network_config(selftest)
    assert res.failed == [] and res.count == 5
    netns.disable = None  # no IPv6 in the kernel at all: fine too
    assert _network_config(selftest).failed == []


@pytest.mark.parametrize(
    "field, value, failed",
    [
        ("ifaces", ["lo", "eth0", "eth1"], "interfaces are lo + eth0 only"),
        ("ifaces", ["lo"], "interfaces are lo + eth0 only"),
        ("default", True, "no IPv4 default route"),
        ("v6", ["::1", "fe80::1"], "no IPv6 address but ::1"),
        ("disable", "0", "IPv6 disabled on eth0"),
        ("own", "10.213.250.5", "own address in the box range"),
        ("own", None, "own address in the box range"),
    ],
    ids=["extra-iface", "no-eth0", "default-route", "ipv6", "ipv6-on", "outside-range", "no-address"],
)
def test_selftest_network_config_flags(selftest, netns, field, value, failed):
    setattr(netns, field, value)
    assert _network_config(selftest).failed == [failed]


def test_selftest_box_range_must_parse(selftest, netns, monkeypatch):
    monkeypatch.setenv("JESS_CODER_BOX_RANGE", "junk")
    assert _network_config(selftest).failed == ["own address in the box range"]
    monkeypatch.delenv("JESS_CODER_BOX_RANGE")
    assert _network_config(selftest).count == 4  # no range given: that check is skipped


@pytest.fixture
def reach(selftest, monkeypatch):
    """check_reach with _run_all recorded (and answering from ``answers``); nothing is probed."""
    st = types.SimpleNamespace(calls=[], answers={})

    def fake_run_all(fn, items, timeout, workers=64):
        items = list(items)
        st.calls.append({"fn": fn.__name__, "items": items, "timeout": timeout})
        return st.answers.get(len(st.calls), ([], 0))

    monkeypatch.setattr(selftest, "_run_all", fake_run_all)
    monkeypatch.setenv("JESS_SELFTEST_HOSTS", "10.213.250.2,192.0.2.10")
    monkeypatch.setenv("JESS_CODER_SUBNET", "10.213.250.0/24")
    monkeypatch.setenv("JESS_SELFTEST_HOSTS_V6", "fe80::1,2001:db8::1")
    return st


def _probes(call: dict) -> dict:
    by_host: dict = {}
    for host, port, family, expect in call["items"]:
        by_host.setdefault(host, set()).add((port, family, expect))
    return by_host


def test_selftest_reach_expectations_per_address(selftest, reach):
    res = selftest.Results()
    selftest.check_reach(res, "10.213.250.2", 8888)
    assert res.failed == []
    assert [c["fn"] for c in reach.calls] == ["_resolves", "_tcp_reach", "_tcp_reach", "_udp_dns_answers"]
    ports = set(selftest.PROBE_PORTS)
    tcp = _probes(reach.calls[1])
    v4, v6 = socket.AF_INET, socket.AF_INET6
    assert tcp["192.0.2.10"] == {(p, v4, "no_route") for p in ports}
    for public in selftest.PUBLIC_V4:
        assert tcp[public] == {(p, v4, "no_route") for p in ports}
    for public in selftest.PUBLIC_V6:
        assert tcp[public] == {(p, v6, "no_route") for p in ports}
    assert tcp["fe80::1%eth0"] == {(p, v6, "no_route") for p in ports}  # only link-local v6 from the launcher
    assert tcp["10.213.250.1"] == {(p, v4, "absent") for p in ports}  # the bridge's .1: nobody home
    assert tcp["10.213.250.2"] == {(p, v4, "closed") for p in ports - {8888}}  # gate host: refusals are fine
    assert set(tcp) == {"192.0.2.10", "fe80::1%eth0", "10.213.250.1", "10.213.250.2", *selftest.PUBLIC_V4,
                        *selftest.PUBLIC_V6}  # fmt: skip
    gate = reach.calls[2]["items"]
    assert len(gate) == 65534 and {(h, f, e) for h, _p, f, e in gate} == {("10.213.250.2", v4, "closed")}
    assert 8888 not in {p for _h, p, _f, _e in gate}
    assert reach.calls[3]["items"] == ["10.213.250.2", "192.0.2.10", *selftest.PUBLIC_V4]


@pytest.mark.parametrize("call, check", [(1, "external DNS"), (2, "no TCP reach"), (3, "gate host"), (4, "no UDP DNS")])
@pytest.mark.parametrize("answer", [(["hit"], 0), ([], 1)], ids=["hit", "unfinished"])
def test_selftest_reach_fails_on_a_hit_or_an_unfinished_probe(selftest, reach, call, check, answer):
    reach.answers[call] = answer
    res = selftest.Results()
    selftest.check_reach(res, "10.213.250.2", 8888)
    assert len(res.failed) == 1 and res.failed[0].startswith(check)


def test_selftest_reach_needs_host_addresses(selftest, reach, monkeypatch):
    monkeypatch.setenv("JESS_SELFTEST_HOSTS", "bad,127.0.0.1")
    res = selftest.Results()
    selftest.check_reach(res, "10.213.250.2", 8888)
    assert res.failed == ["launcher passed host addresses to try"]


def test_selftest_reach_caps_the_host_list(selftest, reach, monkeypatch):
    hosts = [f"10.1.{i // 250}.{i % 250 + 1}" for i in range(300)]
    monkeypatch.setenv("JESS_SELFTEST_HOSTS", ",".join(hosts))
    res = selftest.Results()
    selftest.check_reach(res, "10.213.250.2", 8888)
    assert res.failed == [f"at most {selftest.MAX_HOSTS} host addresses"]
    probed = {h for h, *_ in reach.calls[1]["items"]}
    assert set(hosts[: selftest.MAX_HOSTS]) <= probed and not probed & set(hosts[selftest.MAX_HOSTS :])


def test_selftest_run_all_counts_hits_and_stragglers(selftest):
    release = threading.Event()

    def probe(item):
        if item == "slow":
            release.wait(10)
            return None
        return f"hit {item}" if item in ("b", "a") else None

    try:
        hits, pending = selftest._run_all(probe, ["b", "x", "a", "slow"], 0.5)
    finally:
        release.set()
    assert hits == ["hit a", "hit b"] and pending == 1


def _gate_http(health=403, chat=403, dotdot=404, no_token=401, bad_token=401, good_token=200):
    def fake(host, port, method, path, token=None):
        if path == "/health":
            return health
        if path == "/v1/chat/completions":
            return chat
        if ".." in path:
            return dotdot
        if token is None:
            return no_token
        return bad_token if token == "jcs_not-a-real-token" else good_token

    return fake


@pytest.mark.parametrize(
    "over, failed",
    [
        ({}, []),
        ({"health": None}, ["gate address answers"]),
        ({"health": 200}, ["other paths are refused (403)"]),
        ({"chat": 200}, ["POST /v1/chat/completions refused"]),
        ({"dotdot": 200}, ["GET /api/coder/v1/models/../../health refused"]),
        ({"no_token": 404}, ["gate wants a coder token (401)"]),
        ({"no_token": 200}, ["gate wants a coder token (401)"]),
        ({"bad_token": 200}, ["gate refuses a bad token (401)"]),
        ({"good_token": 401}, ["session token works (200)"]),
    ],
    ids=[
        "healthy",
        "no-answer",
        "health-open",
        "chat-open",
        "dotdot",
        "gate-off",
        "no-auth",
        "bad-token",
        "key-refused",
    ],
)
def test_selftest_gate_checks(selftest, monkeypatch, over, failed):
    monkeypatch.setattr(selftest, "_http", _gate_http(**over))
    monkeypatch.setenv("JESS_BRAIN_KEY", "jcs_real")
    res = selftest.Results()
    selftest.check_gate(res, "10.213.250.2", 8888)
    assert res.failed == failed
    if over.get("health", 403) is None:
        assert res.count == 1  # nothing else is tried
    else:
        assert res.count == 7


def test_selftest_gate_off_says_how_to_fix_it(selftest, monkeypatch, capsys):
    monkeypatch.setattr(selftest, "_http", _gate_http(no_token=404))
    monkeypatch.delenv("JESS_BRAIN_KEY", raising=False)
    res = selftest.Results()
    selftest.check_gate(res, "10.213.250.2", 8888)
    assert res.count == 6  # no token: the "token works" check is skipped
    assert "CODER_ENABLED=true" in capsys.readouterr().out


def _settings_url(url: str) -> str:
    return json.dumps({"modelProviders": {"openai": [{"baseUrl": url}]}})


_STEPS = ("check_identity", "check_filesystem", "check_network_config", "check_reach", "check_gate")


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        '{"modelProviders": {}}',
        '{"modelProviders": {"openai": []}}',
        '{"modelProviders": {"openai": "x"}}',
        _settings_url("https://10.213.250.2:8888/api/coder/v1"),
        _settings_url("http://gate.example:8888/api/coder/v1"),
        _settings_url("http://10.213.250.2:8888/v1"),
    ],
    ids=["missing", "not-json", "list", "no-openai", "empty-list", "string", "https", "hostname", "wrong-path"],
)
def test_selftest_main_fails_closed_on_bad_settings(selftest, tmp_path, monkeypatch, capsys, content):
    path = tmp_path / "settings.json"
    if content is not None:
        path.write_text(content)
    monkeypatch.setattr(selftest, "SETTINGS", str(path))
    ran = []
    for step in _STEPS:
        monkeypatch.setattr(selftest, step, lambda *a, _s=step: ran.append(_s))
    assert selftest.main() == 1
    out = capsys.readouterr().out
    assert "FAIL settings:" in out and out.rstrip().endswith("selftest: FAIL")
    assert ran == []


def test_selftest_main_runs_every_check_and_fails_on_a_crash(selftest, tmp_path, monkeypatch, capsys):
    path = tmp_path / "settings.json"
    path.write_text(_settings_url("http://10.213.250.2:8888/api/coder/v1"))
    monkeypatch.setattr(selftest, "SETTINGS", str(path))
    ran = []

    def ok(name):
        def step(res, *args):
            ran.append((name, args))
            res.check(name, True)

        return step

    def crash(res, *args):
        ran.append(("check_filesystem", args))
        raise RuntimeError("boom")

    for step in _STEPS:
        monkeypatch.setattr(selftest, step, ok(step))
    monkeypatch.setattr(selftest, "check_filesystem", crash)
    assert selftest.main() == 1
    gate = ("10.213.250.2", 8888)
    assert ran == [("check_identity", ()), ("check_filesystem", ()), ("check_network_config", gate[:1]),
                   ("check_reach", gate), ("check_gate", gate)]  # fmt: skip
    out = capsys.readouterr().out
    assert "FAIL check crashed (RuntimeError): boom" in out and "selftest: FAIL (1 of 5 checks" in out
    monkeypatch.setattr(selftest, "check_filesystem", ok("check_filesystem"))
    assert selftest.main() == 0
    assert "selftest: PASS (5 checks" in capsys.readouterr().out
