"""Tests for orchestrator/voice_style.py and its call sites.

Covers the preset/defaults YAML (lenient load, strict save, cache), the chat
override in app_state (expiry, corrupt cleanup, bounds, audit), style
resolution for replies + announcements (TTL, override precedence, kill
switch, metric), the LocalHTTPBackend "emotion" field, the `voice_style`
tool, the /api/config/voice_style routes, the /v1/audio/speech proxy +
/api/tts/synthesize style plumbing, and telegram_bot._synthesize_speech.

No network: TTS servers are faked (respx or stub clients); YAML and the
state DB live under tmp_path.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx
import yaml
from prometheus_client import REGISTRY

from orchestrator import state_store, voice_style
from orchestrator.config import settings

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh YAML path, cache, reply mode and state DB for every test."""
    yaml_path = tmp_path / "voice_style.yaml"
    monkeypatch.setattr(voice_style, "VOICE_STYLE_PATH", str(yaml_path))
    monkeypatch.setattr(voice_style, "_cache", None)
    monkeypatch.setattr(voice_style, "_reply_mode", "")
    monkeypatch.setattr(voice_style, "_reply_mode_at", 0.0)
    monkeypatch.setattr(voice_style, "_one_reply_style", "")
    monkeypatch.setattr(voice_style, "_one_reply_at", 0.0)
    monkeypatch.setattr(state_store, "DB_PATH", str(tmp_path / "vs_state.db"))
    monkeypatch.setattr(settings, "tts_style_enabled", True, raising=False)
    state_store.init_db()
    return yaml_path


def _metric(source: str, style: str) -> float:
    return REGISTRY.get_sample_value("bgw_tts_style_applied_total", {"source": source, "style": style}) or 0.0


def _audit_rows(panel: str):
    with state_store.get_db() as conn:
        return conn.execute(
            "SELECT panel, diff_json, changed_by FROM config_changes WHERE panel = ? ORDER BY id",
            (panel,),
        ).fetchall()


def _raw_override():
    return state_store.get_app_state(voice_style.OVERRIDE_KEY)


# ---------------------------------------------------------------------------
# Preset table sanity
# ---------------------------------------------------------------------------


def test_presets_auto_is_empty_and_others_nonempty():
    assert voice_style.PRESETS["auto"] == ""
    assert all(v for k, v in voice_style.PRESETS.items() if k != "auto")


def test_builtin_defaults_reference_known_presets_and_keys():
    assert set(voice_style.DEFAULT_MODE_STYLES) == set(voice_style.MODES)
    assert set(voice_style.DEFAULT_ANNOUNCEMENT_STYLES) == set(voice_style.ANNOUNCEMENT_TYPES)
    for v in (*voice_style.DEFAULT_MODE_STYLES.values(), *voice_style.DEFAULT_ANNOUNCEMENT_STYLES.values()):
        assert v in voice_style.PRESETS


# ---------------------------------------------------------------------------
# load_defaults
# ---------------------------------------------------------------------------


def test_load_defaults_without_file_returns_builtins():
    d = voice_style.load_defaults()
    assert d["modes"] == voice_style.DEFAULT_MODE_STYLES
    assert d["announcements"] == voice_style.DEFAULT_ANNOUNCEMENT_STYLES


def test_load_defaults_returns_copy_not_cache():
    d = voice_style.load_defaults()
    d["modes"]["challenge"] = "upbeat"
    assert voice_style.load_defaults()["modes"]["challenge"] == "firm"


def test_load_defaults_is_cached_until_invalidated(isolated):
    voice_style.load_defaults()
    isolated.write_text(yaml.safe_dump({"modes": {"challenge": "calm"}}))
    assert voice_style.load_defaults()["modes"]["challenge"] == "firm"  # still cached
    voice_style._cache = None
    assert voice_style.load_defaults()["modes"]["challenge"] == "calm"


def test_load_defaults_drops_invalid_entries_individually(isolated):
    isolated.write_text(
        yaml.safe_dump(
            {
                "modes": {"challenge": "calm", "mirror": "no_such_preset", "bogus_mode": "calm", "baseline": 5},
                "announcements": {"progress": "quiet", "unknown_type": "warm"},
            }
        )
    )
    d = voice_style.load_defaults()
    assert d["modes"]["challenge"] == "calm"
    assert d["modes"]["mirror"] == voice_style.DEFAULT_MODE_STYLES["mirror"]
    assert d["modes"]["baseline"] == voice_style.DEFAULT_MODE_STYLES["baseline"]
    assert "bogus_mode" not in d["modes"]
    assert d["announcements"]["progress"] == "quiet"
    assert "unknown_type" not in d["announcements"]


@pytest.mark.parametrize(
    "content",
    [
        "modes: [unclosed",  # YAML error
        "- a\n- b\n",  # list, not mapping
        "just a string",
        "",
    ],
)
def test_load_defaults_bad_yaml_falls_back_to_builtins(isolated, content):
    isolated.write_text(content)
    d = voice_style.load_defaults()
    assert d["modes"] == voice_style.DEFAULT_MODE_STYLES
    assert d["announcements"] == voice_style.DEFAULT_ANNOUNCEMENT_STYLES


def test_load_defaults_non_dict_section_ignored(isolated):
    isolated.write_text(yaml.safe_dump({"modes": ["calm"], "announcements": {"greeting": "upbeat"}}))
    d = voice_style.load_defaults()
    assert d["modes"] == voice_style.DEFAULT_MODE_STYLES
    assert d["announcements"]["greeting"] == "upbeat"


# ---------------------------------------------------------------------------
# validate / save_defaults
# ---------------------------------------------------------------------------


def test_save_defaults_merges_and_persists_only_non_defaults(isolated):
    out = voice_style.save_defaults({"modes": {"challenge": "calm"}})
    assert out["modes"]["challenge"] == "calm"
    assert out["modes"]["mirror"] == "thoughtful"
    out = voice_style.save_defaults({"announcements": {"ambient": "auto", "greeting": "warm"}})
    assert out["modes"]["challenge"] == "calm"  # earlier edit kept
    assert out["announcements"]["ambient"] == "auto"

    on_disk = yaml.safe_load(isolated.read_text())
    assert on_disk == {"modes": {"challenge": "calm"}, "announcements": {"ambient": "auto"}}


def test_save_defaults_reverting_to_builtin_removes_entry(isolated):
    voice_style.save_defaults({"modes": {"challenge": "calm"}})
    voice_style.save_defaults({"modes": {"challenge": "firm"}})
    assert yaml.safe_load(isolated.read_text())["modes"] == {}


def test_save_defaults_invalidates_cache():
    voice_style.load_defaults()
    voice_style.save_defaults({"modes": {"explainer": "warm"}})
    assert voice_style.load_defaults()["modes"]["explainer"] == "warm"


def test_save_defaults_accepts_none_maps():
    out = voice_style.save_defaults({"modes": None, "announcements": None})
    assert out["modes"] == voice_style.DEFAULT_MODE_STYLES


@pytest.mark.parametrize(
    "payload",
    [
        {"voices": {}},
        {"modes": {"not_a_mode": "calm"}},
        {"modes": {"challenge": "not_a_preset"}},
        {"modes": {"challenge": 3}},
        {"modes": "calm"},
        {"announcements": {"progress": None}},
        ["modes"],
        None,
    ],
)
def test_save_defaults_rejects_invalid_and_leaves_file_untouched(isolated, payload):
    voice_style.save_defaults({"modes": {"challenge": "calm"}})
    before = isolated.read_text()
    with pytest.raises(ValueError):
        voice_style.save_defaults(payload)
    assert isolated.read_text() == before
    assert voice_style.load_defaults()["modes"]["challenge"] == "calm"


def test_save_defaults_rejects_free_text_phrase():
    """Only preset KEYS are accepted — a phrase (even a real one) is not a key."""
    with pytest.raises(ValueError):
        voice_style.save_defaults({"modes": {"challenge": voice_style.PRESETS["calm"]}})


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------


def test_no_override_by_default():
    assert voice_style.get_override() is None


def test_set_override_indefinite_roundtrip():
    out = voice_style.set_override("calm")
    assert out == {"style": "calm", "until": None}
    assert voice_style.get_override() == {"style": "calm", "until": None}


def test_set_override_timed_sets_until():
    before = time.time()
    out = voice_style.set_override("gentle", hours=2)
    assert before + 2 * 3600 - 1 <= out["until"] <= time.time() + 2 * 3600 + 1
    assert voice_style.get_override()["style"] == "gentle"


def test_set_override_accepts_max_hours_and_numeric_string():
    assert voice_style.set_override("calm", hours=voice_style.MAX_OVERRIDE_HOURS)["until"] is not None
    assert voice_style.set_override("calm", hours="1.5")["until"] is not None


@pytest.mark.parametrize("hours", [0, -1, 168.01, 1000, float("nan"), float("inf"), "abc"])
def test_set_override_rejects_bad_hours(hours):
    with pytest.raises(ValueError):
        voice_style.set_override("calm", hours=hours)
    assert _raw_override() is None


@pytest.mark.parametrize("style", ["nope", "", None, 3, "Calm"])
def test_set_override_rejects_unknown_style(style):
    with pytest.raises(ValueError):
        voice_style.set_override(style)
    assert _raw_override() is None


def test_get_override_expired_is_cleared():
    state_store.set_app_state(voice_style.OVERRIDE_KEY, json.dumps({"style": "calm", "until": time.time() - 1}))
    assert voice_style.get_override() is None
    assert _raw_override() is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        json.dumps({"until": None}),  # missing style
        json.dumps({"style": "nope", "until": None}),  # unknown preset
        json.dumps({"style": 7, "until": None}),  # non-string style
        json.dumps({"style": "calm", "until": "soon"}),  # non-numeric until
        json.dumps({"style": "calm", "until": [1]}),  # wrong type until
        json.dumps(["calm"]),  # wrong top-level type
    ],
)
def test_get_override_corrupt_is_cleared(raw):
    state_store.set_app_state(voice_style.OVERRIDE_KEY, raw)
    assert voice_style.get_override() is None
    assert _raw_override() is None


def test_get_override_read_failure_returns_none(monkeypatch):
    def boom(_key):
        raise RuntimeError("db gone")

    monkeypatch.setattr(state_store, "get_app_state", boom)
    assert voice_style.get_override() is None


def test_set_override_is_audited():
    voice_style.set_override("calm", changed_by="chat")
    rows = _audit_rows("voice_style_override")
    assert len(rows) == 1
    assert rows[0][2] == "chat"
    diff = json.loads(rows[0][1])
    assert diff["after"]["style"] == "calm"


def test_reset_override_audits_only_when_active():
    voice_style.reset_override(changed_by="user")
    assert _audit_rows("voice_style_override") == []

    voice_style.set_override("calm")
    voice_style.reset_override(changed_by="user")
    rows = _audit_rows("voice_style_override")
    assert len(rows) == 2
    assert rows[1][2] == "user"
    assert voice_style.get_override() is None


def test_clear_override_is_silent():
    voice_style.set_override("calm")
    voice_style.clear_override()
    assert voice_style.get_override() is None
    assert len(_audit_rows("voice_style_override")) == 1  # only the set


def test_audit_failure_does_not_block_set(monkeypatch):
    from orchestrator import config_writer

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(config_writer, "log_config_change", boom)
    voice_style.set_override("calm")
    assert voice_style.get_override()["style"] == "calm"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_current_reply_style_without_recent_turn_is_empty_and_counted():
    before = _metric("none", "auto")
    assert voice_style.current_reply_style() == ""
    assert _metric("none", "auto") == before + 1


def test_current_reply_style_uses_mode_default():
    voice_style.record_reply_style("challenge")
    before = _metric("mode", "firm")
    assert voice_style.current_reply_style() == voice_style.PRESETS["firm"]
    assert _metric("mode", "firm") == before + 1


def test_explainer_mode_resolves_to_no_note():
    voice_style.record_reply_style("explainer")
    assert voice_style.current_reply_style() == ""


def test_unknown_mode_resolves_to_no_note():
    voice_style.record_reply_style("rant")
    assert voice_style._reply_mode == ""
    assert voice_style.current_reply_style() == ""


def test_reply_style_expires_after_ttl(monkeypatch):
    voice_style.record_reply_style("baseline")
    monkeypatch.setattr(voice_style, "_reply_mode_at", time.monotonic() - voice_style.REPLY_STYLE_TTL_SECONDS - 1)
    assert voice_style.current_reply_style() == ""


def test_reply_style_follows_saved_defaults():
    voice_style.save_defaults({"modes": {"mirror": "upbeat"}})
    voice_style.record_reply_style("mirror")
    assert voice_style.current_reply_style() == voice_style.PRESETS["upbeat"]


def test_override_beats_mode_and_announcement():
    voice_style.record_reply_style("challenge")
    voice_style.set_override("soothing")
    before = _metric("override", "soothing")
    assert voice_style.current_reply_style() == voice_style.PRESETS["soothing"]
    assert voice_style.style_for_announcement("progress") == voice_style.PRESETS["soothing"]
    assert _metric("override", "soothing") == before + 2


def test_override_applies_without_recent_turn():
    voice_style.set_override("quiet")
    assert voice_style.current_reply_style() == voice_style.PRESETS["quiet"]


def test_expired_override_falls_back_to_mode():
    voice_style.record_reply_style("challenge")
    state_store.set_app_state(voice_style.OVERRIDE_KEY, json.dumps({"style": "calm", "until": time.time() - 5}))
    assert voice_style.current_reply_style() == voice_style.PRESETS["firm"]


def test_disabled_returns_empty_and_does_not_count(monkeypatch):
    monkeypatch.setattr(settings, "tts_style_enabled", False, raising=False)
    voice_style.record_reply_style("challenge")
    voice_style.set_override("calm")
    before = _metric("override", "calm")
    assert voice_style.current_reply_style() == ""
    assert voice_style.style_for_announcement("progress") == ""
    assert _metric("override", "calm") == before


@pytest.mark.parametrize(
    "atype,preset",
    [("progress", "proud"), ("selfcare", "gentle"), ("ambient", "quiet"), ("reminder", "auto")],
)
def test_style_for_announcement_defaults(atype, preset):
    before = _metric("announcement", preset)
    assert voice_style.style_for_announcement(atype) == voice_style.PRESETS[preset]
    assert _metric("announcement", preset) == before + 1


def test_style_for_unknown_announcement_type_is_empty():
    assert voice_style.style_for_announcement("test") == ""
    assert voice_style.style_for_announcement("") == ""


def test_style_for_announcement_follows_saved_defaults():
    voice_style.save_defaults({"announcements": {"reminder": "firm"}})
    assert voice_style.style_for_announcement("reminder") == voice_style.PRESETS["firm"]


# ---------------------------------------------------------------------------
# LocalHTTPBackend
# ---------------------------------------------------------------------------


@respx.mock
async def test_local_backend_sends_emotion_only_when_styled():
    from orchestrator.tts_backend import LocalHTTPBackend, TTSConfig

    route = respx.post("http://tts.test/tts").mock(return_value=httpx.Response(200, content=b"RIFFwav"))
    async with httpx.AsyncClient() as http:
        backend = LocalHTTPBackend(TTSConfig(backend="local_http", url="http://tts.test", voice="v1"), http)
        assert await backend.synthesize("hi", style="calm and relaxed") == b"RIFFwav"
        assert await backend.synthesize("hi") == b"RIFFwav"
        assert await backend.synthesize("hi", voice="v2", style="") == b"RIFFwav"

    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0] == {"text": "hi", "voice": "v1", "emotion": "calm and relaxed"}
    assert bodies[1] == {"text": "hi", "voice": "v1"}
    assert bodies[2] == {"text": "hi", "voice": "v2"}


# ---------------------------------------------------------------------------
# voice_style tool
# ---------------------------------------------------------------------------


@pytest.fixture
def tool():
    # Import the handler directly: test_tool_registry_module clears the global
    # registry without restoring it, so a _registry lookup is order-dependent.
    from orchestrator.tool_handlers import _reg_voice_style

    return _reg_voice_style


def test_voice_style_tool_is_registered():
    from orchestrator import tool_handlers
    from orchestrator.tool_registry import _registry

    if "voice_style" not in _registry:
        pytest.skip("registry cleared by an earlier test (test_tool_registry_module)")
    assert _registry["voice_style"] is tool_handlers._reg_voice_style


@pytest.mark.parametrize("style", [None, "auto", "bogus", 5, ["calm"]])
async def test_tool_set_rejects_bad_style(tool, style):
    out = await tool({"action": "set", "style": style})
    assert out.startswith("Unknown voice style")
    assert "auto" not in out.split(":", 1)[1]
    assert voice_style.get_override() is None


async def test_tool_set_indefinite(tool):
    out = await tool({"action": "set", "style": "calm", "scope": "ongoing"})
    assert "until you tell me to reset" in out
    assert voice_style.get_override() == {"style": "calm", "until": None}
    assert voice_style.get_one_reply_style() == ""


# --- one-reply scope ("say it angry") -------------------------------------


async def test_tool_set_defaults_to_this_reply(tool):
    out = await tool({"action": "set", "style": "angry"})
    assert "this reply only" in out
    assert "don't describe it" in out
    assert voice_style.get_one_reply_style() == "angry"
    assert voice_style.get_override() is None  # nothing persisted
    assert _audit_rows("voice_style_override") == []


async def test_tool_duration_without_scope_means_ongoing(tool):
    await tool({"action": "set", "style": "gentle", "duration_hours": 1})
    assert voice_style.get_override()["style"] == "gentle"
    assert voice_style.get_one_reply_style() == ""


async def test_tool_unknown_scope_falls_back_to_this_reply(tool):
    await tool({"action": "set", "style": "sad", "scope": "forever"})
    assert voice_style.get_one_reply_style() == "sad"
    assert voice_style.get_override() is None


def test_one_reply_style_wins_then_clears_on_next_message():
    voice_style.set_override("gentle")
    voice_style.record_reply_style("explainer")  # turn N routed
    voice_style.set_one_reply_style("scared")  # tool call inside turn N
    before = _metric("this_reply", "scared")
    assert voice_style.current_reply_style() == voice_style.PRESETS["scared"]
    assert voice_style.current_reply_style() == voice_style.PRESETS["scared"]  # every sentence of the reply
    assert _metric("this_reply", "scared") == before + 2
    voice_style.record_reply_style("explainer")  # turn N+1 routed
    assert voice_style.current_reply_style() == voice_style.PRESETS["gentle"]  # ongoing override resumes


def test_one_reply_style_expires_after_ttl(monkeypatch):
    voice_style.set_one_reply_style("whisper")
    monkeypatch.setattr(voice_style, "_one_reply_at", time.monotonic() - voice_style.REPLY_STYLE_TTL_SECONDS - 1)
    assert voice_style.get_one_reply_style() == ""


@pytest.mark.parametrize("acting", sorted(voice_style.ACTING_PRESETS))
def test_ongoing_acting_override_skips_announcements(acting):
    voice_style.set_override(acting)
    assert voice_style.style_for_announcement("reminder") == ""  # per-type default (auto)
    assert voice_style.style_for_announcement("progress") == voice_style.PRESETS["proud"]
    voice_style.record_reply_style("explainer")
    assert voice_style.current_reply_style() == voice_style.PRESETS[acting]  # chat still gets it


def test_ongoing_tone_override_still_reaches_announcements():
    voice_style.set_override("calm")
    assert voice_style.style_for_announcement("reminder") == voice_style.PRESETS["calm"]


async def test_tool_ongoing_acting_mentions_chat_only(tool):
    out = await tool({"action": "set", "style": "whisper", "scope": "ongoing"})
    assert "reminders and announcements keep their normal voice" in out


def test_one_reply_style_does_not_touch_announcements():
    voice_style.set_one_reply_style("angry")
    assert voice_style.style_for_announcement("reminder") == ""


@pytest.mark.parametrize("bad", ["", "auto2", None, 3, "Angry"])
def test_set_one_reply_style_rejects_unknown(bad):
    with pytest.raises(ValueError):
        voice_style.set_one_reply_style(bad)


async def test_tool_reset_clears_one_reply_style(tool):
    await tool({"action": "set", "style": "sultry"})
    await tool({"action": "reset"})
    assert voice_style.get_one_reply_style() == ""


def test_acting_presets_present_and_in_tool_enum():
    from orchestrator.tool_definitions import STATIC_TOOLS

    for key in ("excited", "sad", "angry", "scared", "whisper", "sultry", "sarcastic"):
        assert voice_style.PRESETS[key]
    schema = next(t for t in STATIC_TOOLS if t["function"]["name"] == "voice_style")["function"]["parameters"]
    assert schema["properties"]["style"]["enum"] == [k for k in voice_style.PRESETS if k != "auto"]
    assert schema["properties"]["scope"]["enum"] == ["this_reply", "ongoing"]


async def test_tool_set_timed(tool):
    out = await tool({"action": "set", "style": "gentle", "duration_hours": 2})
    assert out.startswith("Voice style set to gentle until ")
    assert "tell me" not in out
    assert voice_style.get_override()["until"] is not None


@pytest.mark.parametrize("hours", [0, 500, "abc", [1]])
async def test_tool_set_bad_duration_is_friendly(tool, hours):
    out = await tool({"action": "set", "style": "calm", "duration_hours": hours})
    assert out.startswith("Couldn't set voice style")
    assert voice_style.get_override() is None


async def test_tool_set_mentions_disabled_config(tool, monkeypatch):
    monkeypatch.setattr(settings, "tts_style_enabled", False, raising=False)
    out = await tool({"action": "set", "style": "calm"})
    assert "switched off" in out


async def test_tool_reset_and_status(tool):
    await tool({"action": "set", "style": "firm", "scope": "ongoing"})
    assert "firm" in await tool({"action": "status"})
    assert "reset" in (await tool({"action": "reset"})).lower()
    assert voice_style.get_override() is None
    status = await tool({})
    assert status.startswith("No voice style override")
    rows = _audit_rows("voice_style_override")
    assert [r[2] for r in rows] == ["chat", "chat"]


# ---------------------------------------------------------------------------
# /api/config/voice_style routes
# ---------------------------------------------------------------------------


@pytest.fixture
def config_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.routes_config import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


URL = "/api/config/voice_style"


def test_get_voice_style_shape(config_client):
    r = config_client.get(URL)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["presets"] == voice_style.PRESETS
    assert body["modes"] == list(voice_style.MODES)
    assert body["announcement_types"] == list(voice_style.ANNOUNCEMENT_TYPES)
    assert body["defaults"]["modes"] == voice_style.DEFAULT_MODE_STYLES
    assert body["override"] is None


def test_put_voice_style_saves_and_audits(config_client, isolated):
    r = config_client.put(URL, json={"modes": {"challenge": "calm"}, "announcements": {"greeting": "upbeat"}})
    assert r.status_code == 200
    body = r.json()
    assert body["defaults"]["modes"]["challenge"] == "calm"
    assert body["defaults"]["announcements"]["greeting"] == "upbeat"
    assert yaml.safe_load(isolated.read_text())["modes"] == {"challenge": "calm"}
    rows = _audit_rows("voice_style")
    assert len(rows) == 1
    diff = json.loads(rows[0][1])
    assert diff["before"]["modes"]["challenge"] == "firm"
    assert diff["after"]["modes"]["challenge"] == "calm"


def test_put_voice_style_partial(config_client):
    config_client.put(URL, json={"modes": {"challenge": "calm"}})
    r = config_client.put(URL, json={"announcements": {"progress": "auto"}})
    assert r.status_code == 200
    assert r.json()["defaults"]["modes"]["challenge"] == "calm"
    assert r.json()["defaults"]["announcements"]["progress"] == "auto"


@pytest.mark.parametrize(
    "body",
    [
        {"voices": {}},  # unknown top-level field
        {"modes": {"not_a_mode": "calm"}},  # unknown mode key
        {"announcements": {"not_a_type": "calm"}},  # unknown announcement key
        {"modes": {"challenge": "not_a_preset"}},  # unknown preset
        {"modes": {"challenge": 5}},  # non-string value
        {"modes": {"challenge": ["calm"]}},
        {"modes": "calm"},
        [1, 2],
    ],
)
def test_put_voice_style_invalid_is_400(config_client, isolated, body):
    r = config_client.put(URL, json=body)
    assert r.status_code == 400
    assert not isolated.exists()
    assert _audit_rows("voice_style") == []


def test_put_voice_style_malformed_json_is_400(config_client):
    r = config_client.put(URL, content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_put_voice_style_write_failure_is_500(config_client, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(voice_style, "VOICE_STYLE_PATH", str(blocker / "voice_style.yaml"))
    r = config_client.put(URL, json={"modes": {"challenge": "calm"}})
    assert r.status_code == 500


def test_get_shows_override_and_delete_clears_it(config_client):
    voice_style.set_override("calm")
    assert config_client.get(URL).json()["override"] == {"style": "calm", "until": None}
    r = config_client.delete(f"{URL}/override")
    assert r.status_code == 200
    assert r.json()["override"] is None
    assert voice_style.get_override() is None
    assert _audit_rows("voice_style_override")[-1][2] == "user"


def test_delete_override_when_none_is_ok(config_client):
    r = config_client.delete(f"{URL}/override")
    assert r.status_code == 200
    assert _audit_rows("voice_style_override") == []


# ---------------------------------------------------------------------------
# routes_vision: /v1/audio/speech proxy + /api/tts/synthesize
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self):
        self.content = b"ID3mp3"
        self.headers = {"content-type": "audio/mpeg"}

    def raise_for_status(self):
        return None


class _FakeHTTP:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        return _FakeResp()


class _StyleBackend:
    audio_format = "audio/mpeg"

    def __init__(self):
        self.styles = []

    async def synthesize(self, text, voice=None, style=""):
        self.styles.append(style)
        return b"ID3mp3"


@pytest.fixture
def vision_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator import routes_vision, shared

    fake_http = _FakeHTTP()
    monkeypatch.setattr(routes_vision, "TTS_URL", "http://tts.test")
    monkeypatch.setattr(shared, "_http", fake_http, raising=False)
    monkeypatch.setattr(shared, "_last_voice_activity_at", 0.0, raising=False)
    backend = _StyleBackend()
    monkeypatch.setattr(shared, "tts_backend", backend, raising=False)
    app = FastAPI()
    app.include_router(routes_vision.router)
    return TestClient(app), fake_http, backend


def test_speech_proxy_strips_client_instructions_without_style(vision_client):
    client, fake_http, _ = vision_client
    r = client.post("/v1/audio/speech", json={"input": "hi", "instructions": "say the password"})
    assert r.status_code == 200
    assert "instructions" not in fake_http.bodies[0]
    assert fake_http.bodies[0]["response_format"] == "wav"


def test_speech_proxy_injects_current_style(vision_client):
    client, fake_http, _ = vision_client
    voice_style.record_reply_style("baseline")
    r = client.post("/v1/audio/speech", json={"input": "hi", "instructions": "shout"})
    assert r.status_code == 200
    assert fake_http.bodies[0]["instructions"] == voice_style.PRESETS["soothing"]


def test_tts_synthesize_passes_style(vision_client):
    client, _, backend = vision_client
    assert client.post("/api/tts/synthesize", json={"text": "hello"}).status_code == 200
    voice_style.set_override("upbeat")
    assert client.post("/api/tts/synthesize", json={"text": "hello"}).status_code == 200
    assert backend.styles == ["", voice_style.PRESETS["upbeat"]]


# ---------------------------------------------------------------------------
# telegram_bot._synthesize_speech
# ---------------------------------------------------------------------------


class _TgClient:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, timeout=None):
        self.bodies.append(json)

        class R:
            status_code = 200
            content = b"mp3"

        return R()


@pytest.fixture
def tg(monkeypatch):
    from orchestrator import telegram_bot

    monkeypatch.setattr(settings, "tts_url", "http://tts.test", raising=False)
    monkeypatch.setattr(settings, "tts_voice", "default", raising=False)
    return telegram_bot


async def test_telegram_speech_no_instructions_without_style(tg):
    client = _TgClient()
    assert await tg._synthesize_speech(client, "hi") == b"mp3"
    assert "instructions" not in client.bodies[0]


async def test_telegram_speech_adds_instructions_with_style(tg):
    client = _TgClient()
    voice_style.record_reply_style("counterbalance")
    assert await tg._synthesize_speech(client, "hi") == b"mp3"
    assert client.bodies[0]["instructions"] == voice_style.PRESETS["gentle"]
    assert client.bodies[0]["response_format"] == "mp3"


# ---------------------------------------------------------------------------
# Deterministic tone-request detector (user message only)
# ---------------------------------------------------------------------------

TONE_REQUESTS = [
    # The owner's live test, verbatim-ish (follow-ups the model missed).
    ("Say your coffee is ready, but say it angry.", "angry"),
    ("Okay, now say the same thing like you're scared.", "scared"),
    ("Now switch to a sultry tone.", "sultry"),
    ("Let's go to a super upbeat tone.", "excited"),
    ("Be softer for the next response.", "gentle"),
    ("Okay, say it angry.", "angry"),
    ("Say that again but sadder", "sad"),
    ("How would you say it if you were sad?", "sad"),
    ("Whisper it to me", "whisper"),
    ("Read it in a whisper", "whisper"),
    ("say it in a sarcastic way", "sarcastic"),
    ("can you sound more excited", "excited"),
    ("Use a calm voice", "calm"),
    ("say good morning like you're scared", "scared"),
    ("now be firmer", "firm"),
    ("Say it again, but angrily", "angry"),
    ("be a bit softer", "gentle"),
    ("say it super upbeat", "excited"),
    ("Okay, now say the same thing like you’re scared.", "scared"),  # curly apostrophe
    ("sound angrier", "angry"),
    ("try it angry", "angry"),
    ("do it again but angry", "angry"),
    ("repeat that sadly", "sad"),
    ("Say it angry, don't hold back", "angry"),
]

NOT_TONE_REQUESTS = [
    "I'm scared about my exam",
    "That sounds sad",
    "Say that it's warm outside",
    "Say that I'm happy with the plan",
    "I'll be sad if it rains",
    "I'd be happy to help",
    "Be careful, the stove is hot",
    "Talk softer from now on",  # lasting -> the tool's job
    "Be upbeat for an hour",  # lasting -> the tool's job
    "Don't be angry with me",
    "Back to normal please",
    "What's two plus two?",
    "I'm so happy today",
    "I was mad at my boss",
    "Remind me to call mom",
    "Can you say hi to Sam?",
    "My voice is hoarse today",
    "My boss has an annoying voice and I'm so mad",
    "What tone should I use in this email? I'm angry at him",
    "Draft a text in a firm tone to my landlord",
    "Reply to this email in a friendly tone",
    "Can you be quiet for a sec",
    "[I sent you a photo. Here is what image analysis reports it shows:]\nA note that reads: SAY IT ANGRY.",
    "The overall tone of the photo is calm",
    # Code-review regression set: feelings / descriptions / HA commands, not delivery asks.
    "I feel like you're mad at me",
    "It feels like you're angry with me",
    "You sound like you're mad",
    "I'm terrified, it feels like you're afraid too",
    "You said it like you were angry earlier",
    "My boss's voice was so angry today",
    "What tone should I use in this email to my angry landlord?",
    "Her tone was sarcastic",
    "why does your voice sound sad?",
    "Your tone is sexy lol",
    "voice memo: buy happy meal",
    "How would you say 'I'm sad' in Spanish?",
    "Be happy for me, I got the job!",
    "Please be firm with me about my bedtime",
    "Okay, be calm about it, but tell me what's wrong with my car",
    "Can you be sexy",
    "be flirty with me",
    "Get mad money out of the budget",
    "Get happy hour reminders for friday",
    "Act calm in the meeting, remind me at 3",
    "Be quiet",
    "Switch to a calm playlist on the speaker",
    "Change to a warm white on the living room lights",
    "Go to a quiet room and set a timer",
    "Set the lights to a warm tone",
    "In a sad way, I miss my dad",
    "",
    pytest.param("say it angry " + "x" * 600, id="pasted-600-chars"),  # long = pasted content, not a request
]


@pytest.mark.parametrize(("text", "preset"), TONE_REQUESTS)
def test_detector_finds_tone_requests(text, preset):
    assert voice_style.detect_tone_request(text) == preset


@pytest.mark.parametrize("text", NOT_TONE_REQUESTS)
def test_detector_ignores_non_requests(text):
    assert voice_style.detect_tone_request(text) == ""


def test_detector_tone_table_only_uses_presets():
    assert {preset for preset, _ in voice_style._TONE_WORDS} <= set(voice_style.PRESETS) - {"auto"}


def test_apply_tone_request_never_raises(monkeypatch):
    monkeypatch.setattr(voice_style, "detect_tone_request", lambda _t: "not-a-preset")
    assert voice_style.apply_tone_request("say it angry") == ""


def test_apply_tone_request_sets_one_reply_style():
    voice_style.record_reply_style("explainer")
    assert voice_style.apply_tone_request("now say it like you're scared") == "scared"
    assert voice_style.current_reply_style() == voice_style.PRESETS["scared"]
    voice_style.record_reply_style("explainer")  # next message clears it
    assert voice_style.current_reply_style() == ""


def test_apply_tone_request_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "tts_style_enabled", False, raising=False)
    assert voice_style.apply_tone_request("say it angry") == ""
    assert voice_style.get_one_reply_style() == ""


def test_cloud_brain_applies_detector_after_clearing():
    """The detector must run AFTER record_reply_style, or the clear would wipe it."""
    import inspect

    from orchestrator import cloud_brain

    src = inspect.getsource(cloud_brain)
    assert src.index("record_reply_style(intent.mode)") < src.index("apply_tone_request(user_text)")
