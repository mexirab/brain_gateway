"""Coder box (coder/, scripts/jess-coder): static hardening guards + launcher logic.

The box runs model-written shell commands; docs/CODER.md has the threat model.
These tests pin what makes it a box:

- the image: built from the orchestrator image, own uid (no host account has
  it), no orchestrator code baked in, no healthcheck, pinned Qwen Code + Node;
- Qwen Code's locked settings: the four background features that make extra
  brain calls are off, no telemetry/updates, the token comes from the env;
- the compose service is build-only and can't do anything if started;
- every box container comes from ONE function with the hardening flags;
- the patch review refuses files that run before review, odd modes and
  characters a terminal or a reviewer could be fooled by;
- the self-test's TCP probe really notices an open port.

Needs a full checkout (coder/, scripts/); skips otherwise, like
test_coder_hardening.py.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import socket
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


def test_image_builds_from_the_orchestrator_context():
    text = _dockerfile()
    froms = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert froms == ["jess-orchestrator-base"]


def test_image_uid_matches_launcher_and_is_not_a_login(jc):
    m = re.search(r"^ARG CODER_UID=(\d+)$", _dockerfile(), re.M)
    assert m and int(m.group(1)) == jc.BOX_UID
    assert jc.BOX_UID not in (0, 1000, 1001)  # root, the owner/orchestrator, another login on Jupiter


def test_image_has_no_orchestrator_code_or_healthcheck():
    text = _dockerfile()
    assert "rm -rf /app/orchestrator" in text
    assert re.search(r"^HEALTHCHECK NONE$", text, re.M)
    assert re.search(r"^USER coder$", text, re.M)
    assert "PYTHONPATH= " in text or "PYTHONPATH= \\" in text


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
    assert svc["build"]["additional_contexts"] == {"jess-orchestrator-base": "service:orchestrator"}
    assert "${CODER_GATE_ADDRESS:-10.213.250.2}" in svc["build"]["args"]["GATE_URL"]
    assert svc["image"] == "jess-coder:current"


# ------------------------------------------------------------ box command


def _flag_value(cmd: list, flag: str) -> list:
    return [cmd[i + 1] for i, part in enumerate(cmd) if part == flag]


def test_box_command_hardening(jc, cfg, tmp_path):
    cmd = jc.box_command(
        cfg,
        name="demo",
        mode=["session"],
        network=cfg.network,
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
    for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
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
        cfg, name="x-patch", mode=["patch", "a" * 40], network="none", workspace=tmp_path, workspace_ro=True
    )
    assert _flag_value(cmd, "--network") == ["none"]
    assert _flag_value(cmd, "--mount") == [f"type=bind,src={tmp_path},dst=/work,readonly"]
    assert "-it" not in cmd and "-i" not in cmd


@pytest.mark.parametrize("name", ["", "-x", "A", "a b", "a/b", "a" * 33, "../x", "x;rm"])
def test_bad_session_names(jc, name):
    with pytest.raises(jc.Fatal):
        jc.check_name(name)


def test_good_session_name(jc):
    assert jc.check_name("fix-grocery-1") == "fix-grocery-1"


def test_load_env(jc, tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nA=1\nexport B='two words'\nC=\"x=y\"\n\nbad line\n")
    assert jc.load_env(p) == {"A": "1", "B": "two words", "C": "x=y"}


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


# ------------------------------------------------------------------ patch


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".claude/settings.json",
        ".mcp.json",
        "CLAUDE.md",
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
        ".gitattributes",
        "../etc/passwd",
        "/abs",
        "a//b",
        ".git/config",
        "orchestrator/./x.py",
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
        "README.md",
    ],
)
def test_ordinary_paths(jc, path):
    assert jc.refused_reason(path) is None


def _diff(path: str, *header: str, body: str = "+x\n") -> str:
    return "".join(
        [
            f"diff --git a/{path} b/{path}\n",
            *[h + "\n" for h in header],
            f"--- a/{path}\n",
            f"+++ b/{path}\n",
            "@@ -0,0 +1 @@\n",
            body,
        ]
    )


def test_review_flags_refused_path_and_allow_overrides(jc):
    text = _diff(".github/workflows/ci.yml")
    problems, files = jc.review_patch(text, [("1", "0", ".github/workflows/ci.yml")], [])
    assert files == [".github/workflows/ci.yml"] and any("refused" in p for p in problems)
    problems, _ = jc.review_patch(text, [("1", "0", ".github/workflows/ci.yml")], [".github/workflows/ci.yml"])
    assert problems == []
    problems, _ = jc.review_patch(text, [("1", "0", ".github/workflows/ci.yml")], [".github/"])
    assert problems == []


@pytest.mark.parametrize("mode, word", [("100755", "executable"), ("120000", "symlink"), ("160000", "submodule")])
def test_review_flags_modes(jc, mode, word):
    text = _diff("orchestrator/x", f"new file mode {mode}")
    problems, _ = jc.review_patch(text, [("1", "0", "orchestrator/x")], [])
    assert any(word in p for p in problems)


def test_review_flags_mode_change_on_second_file(jc):
    text = _diff("a.py") + _diff("b.py", "old mode 100644", "new mode 100755")
    problems, _ = jc.review_patch(text, [("1", "0", "a.py"), ("1", "0", "b.py")], [])
    assert problems == ["b.py: executable (refused); --allow b.py if you mean it"]


def test_review_flags_binary(jc):
    problems, _ = jc.review_patch(
        "diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\n", [("-", "-", "i.png")], []
    )
    assert any("binary" in p for p in problems)


@pytest.mark.parametrize(
    "line, what",
    [
        ("+print('\x1b]52;c;ZWNobyBoaQ==\x07')", "control character 0x1b"),
        ("+access = 'user‮ ⁦// admin⁩ ⁦'", "bidirectional"),
        ("+x = 1\x08\x08", "control character 0x08"),
        ("+x = '⁡'", "invisible format character"),
    ],
)
def test_review_flags_terminal_tricks(jc, line, what):
    problems = jc.text_problems(_diff("a.py", body=line + "\n"))
    assert problems and what in problems[0]


def test_plain_text_passes(jc):
    assert jc.text_problems(_diff("a.py", body="+x = 'café'  # tab\tok\r\n")) == []


def test_sanitize_for_terminal(jc):
    out = jc.sanitize_for_terminal("a\x1b[2Jb‮c\td\n")
    assert "\x1b" not in out and "‮" not in out
    assert out == "a^[[2Jb<U+202E>c\td\n"


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


def test_patch_git_ignores_workspace_hooks_and_pagers(entry):
    joined = " ".join(entry.SAFE_GIT)
    assert "core.hooksPath=/dev/null" in joined and "core.fsmonitor=false" in joined and "core.pager=cat" in joined


# ---------------------------------------------------------------- selftest


def test_selftest_tcp_probe_detects_an_open_port(selftest):
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert selftest._tcp_connects(("127.0.0.1", port, socket.AF_INET)) == f"127.0.0.1:{port}"
    assert selftest._tcp_connects(("127.0.0.1", port, socket.AF_INET)) is None  # closed again


def test_selftest_parse_hosts(selftest):
    assert selftest._parse_hosts("10.0.0.1, 127.0.0.1,bad,10.0.0.1,,172.17.0.1") == ["10.0.0.1", "172.17.0.1"]


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
