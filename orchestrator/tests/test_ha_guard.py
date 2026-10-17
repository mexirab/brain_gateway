"""Shared Home Assistant guard (ha_guard.check_ha_call) and the paths that
must use it besides the chat tool:

- ``check_ha_call`` — domain, deny list, service allow-list, targeting keys,
  play_media / join argument checks;
- the legacy NLP path ``HomeAssistantClient.execute_command`` (fast path and
  ``POST /api/ha/command`` text) — refused calls never reach HA;
- ``GET /api/ha/entities`` hides denied + unlisted-domain entities;
- ``POST /api/ha/command`` structured body is guarded;
- device-setting entities (HA entity_category config / diagnostic, or a
  camera/motion/LED-style name when the registry is unreadable) are refused on
  the chat and NLP paths, hidden from the tool schema, and still allowed on
  the structured dashboard call.
"""

import json

import httpx
import pytest

from orchestrator import api_routes, ha_guard, ha_registry, shared
from orchestrator.ha_guard import check_ha_call, ha_device_setting
from orchestrator.ha_integration import HomeAssistantClient
from orchestrator.schemas import HACommandRequest

PLUG = "switch.helios_test_plug"

STATES = [
    ("light.kitchen", "Kitchen"),
    ("switch.office_desk", "Office Desk"),
    (PLUG, "Helios Plug"),
    ("switch.router_power", "Router Power"),
    ("lock.front_door", "Front Door"),
    ("cover.garage_door", "Garage Door"),
    ("media_player.living_room_speaker", "Living Room Speaker"),
    ("media_player.blocked_speaker", "Blocked Speaker"),
    ("vacuum.robo", "Robo"),
    ("switch.porch_cam_power_save", "Porch Cam Power Save"),
    ("switch.porch_widget_toggle", "Porch Widget Toggle"),
    ("switch.porch_flood", "Porch Flood"),
]

# Registry categories as ha_registry.get_registry_meta would return them.
REGISTRY = {
    "light.kitchen": {"area": "Kitchen", "category": None, "hidden": False},
    "switch.office_desk": {"area": "Office", "category": None, "hidden": False},
    "switch.porch_cam_power_save": {"area": "Porch", "category": None, "hidden": False},
    "switch.porch_flood": {"area": "Porch", "category": None, "hidden": False},
    "switch.porch_widget_toggle": {"area": "Porch", "category": "config", "hidden": False},
    "switch.desk_plug_status_report": {"area": None, "category": "diagnostic", "hidden": False},
}


@pytest.fixture
def registry(monkeypatch):
    """Registry readable: categories come from it."""
    monkeypatch.setattr(ha_registry, "_cache", dict(REGISTRY))


@pytest.fixture(autouse=True)
def _deny_defaults(monkeypatch):
    monkeypatch.setattr(shared, "HA_TOOL_DENY_ENTITIES", "switch.router_*,media_player.blocked_speaker")
    monkeypatch.setattr(shared, "HELIOS_PLUG_ENTITY", PLUG)
    monkeypatch.setattr(shared, "FOCUS_AUDIO_LOFI_URL", "https://stream.example/lofi.mp3", raising=False)
    monkeypatch.setattr(shared, "FOCUS_AUDIO_COFFEE_URL", "", raising=False)
    monkeypatch.setattr(ha_guard.settings, "orchestrator_url", "http://10.0.0.248:8888")

    async def _no_registry(url, token):
        return {}

    monkeypatch.setattr(api_routes, "get_registry_meta", _no_registry)


class FakeHA:
    def __init__(self):
        self.service_calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[{"entity_id": e, "state": "off", "attributes": {"friendly_name": n}} for e, n in STATES],
            )
        if request.url.path.startswith("/api/services/"):
            self.service_calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=[])
        return httpx.Response(404)


@pytest.fixture
async def client():
    fake = FakeHA()
    c = HomeAssistantClient(
        url="http://ha.test:8123",
        token="t",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    await c.refresh_entities(force=True)
    yield c, fake
    await c._http.aclose()


# ---------------------------------------------------------------------------
# check_ha_call
# ---------------------------------------------------------------------------


class TestCheckHaCall:
    def test_allowed_call_passes_and_strips_targeting(self):
        refusal, data = check_ha_call(
            "light.kitchen", "turn_on", {"brightness": 128, "entity_id": "lock.front_door", "area_id": "x"}
        )
        assert refusal is None
        assert data == {"brightness": 128}

    @pytest.mark.parametrize(
        "entity_id,service,needle",
        [
            ("vacuum.robo", "turn_on", "domain can't be controlled"),
            ("sensor.x", "turn_on", "domain can't be controlled"),
            (PLUG, "turn_off", "can't be controlled from chat"),
            ("light.helios_test_plug", "turn_off", "can't be controlled from chat"),
            ("switch.router_power", "turn_off", "can't be controlled from chat"),
            ("lock.front_door", "unlock", "confirmation step"),
            ("cover.garage_door", "open_cover", "confirmation step"),
            ("scene.x", "apply", "isn't available"),
            ("light.kitchen", "turn_on\n", "must look like"),
            ("light.kitchen; rm", "turn_on", "must look like"),
        ],
    )
    def test_refusals(self, entity_id, service, needle):
        refusal, data = check_ha_call(entity_id, service, {})
        assert refusal and needle in refusal
        assert data == {}

    def test_non_dict_data_refused(self):
        refusal, _ = check_ha_call("light.kitchen", "turn_on", ["x"])
        assert "must be an object" in refusal

    @pytest.mark.parametrize(
        "media_id",
        [
            "media-source://radio_browser/abc",
            "media-source://media_source/local/song.mp3",
            "https://stream.example/lofi.mp3",
            "http://10.0.0.248:8888/api/audio/abc123.wav",
        ],
    )
    def test_play_media_allowed(self, media_id):
        refusal, data = check_ha_call(
            "media_player.living_room_speaker",
            "play_media",
            {"media_content_id": media_id, "media_content_type": "music"},
        )
        assert refusal is None
        assert data["media_content_id"] == media_id

    @pytest.mark.parametrize(
        "media_id",
        [
            "http://10.0.0.1/admin",
            "https://evil.example/x.mp3",
            "http://10.0.0.248:8888/api/audio/../health",
            "http://10.0.0.248:8888/api/audiox/a.wav",
            "http://10.0.0.248:8888.evil.example/api/audio/a.wav",
            "file:///etc/passwd",
            "media-source://tts/cloud?message=hi",
            "media-source://camera/camera.front",
            "media-source://",
            "media-source://media_source/local/../../../etc/passwd",
            "https://stream.example/lofi.mp3?x=1",
            "",
            None,
            123,
        ],
    )
    def test_play_media_refused(self, media_id):
        data = {"media_content_type": "music"}
        if media_id is not None:
            data["media_content_id"] = media_id
        refusal, _ = check_ha_call("media_player.living_room_speaker", "play_media", data)
        assert refusal and refusal.startswith("Failed:")

    def test_play_media_drops_other_keys(self):
        refusal, data = check_ha_call(
            "media_player.living_room_speaker",
            "play_media",
            {
                "media_content_id": "media-source://radio_browser/abc",
                "media_content_type": "music",
                "media": {"media_content_id": "http://attacker/x"},
                "extra": {"thumb": "http://attacker/t.png"},
                "announce": True,
            },
        )
        assert refusal is None
        assert set(data) == {"media_content_id", "media_content_type", "announce"}

    def test_join_drops_other_keys(self):
        refusal, data = check_ha_call(
            "media_player.living_room_speaker", "join", {"group_members": ["media_player.k"], "x": 1}
        )
        assert refusal is None and data == {"group_members": ["media_player.k"]}

    def test_mixed_case_helios_plug_still_denied(self, monkeypatch):
        monkeypatch.setattr(shared, "HELIOS_PLUG_ENTITY", "switch.Helios_Test_Plug")
        refusal, _ = check_ha_call(PLUG, "turn_off", {})
        assert refusal and "can't be controlled" in refusal

    def test_play_media_ignores_unset_stream_url(self, monkeypatch):
        monkeypatch.setattr(ha_guard.settings, "orchestrator_url", "")
        refusal, _ = check_ha_call("media_player.living_room_speaker", "play_media", {"media_content_id": ""})
        assert refusal
        refusal, _ = check_ha_call(
            "media_player.living_room_speaker", "play_media", {"media_content_id": "/api/audio/a.wav"}
        )
        assert refusal

    def test_join_allowed_members(self):
        refusal, data = check_ha_call(
            "media_player.living_room_speaker", "join", {"group_members": ["media_player.kitchen"]}
        )
        assert refusal is None
        assert data["group_members"] == ["media_player.kitchen"]

    def test_join_string_member_normalised(self):
        refusal, data = check_ha_call(
            "media_player.living_room_speaker", "join", {"group_members": "media_player.kitchen"}
        )
        assert refusal is None
        assert data["group_members"] == ["media_player.kitchen"]

    @pytest.mark.parametrize(
        "members",
        [
            ["media_player.blocked_speaker"],
            ["switch.office_desk"],
            [PLUG],
            ["media_player.ok", "light.kitchen"],
            ["media_player.bad id"],
            [],
            None,
            [1],
            [f"media_player.s{i}" for i in range(17)],
        ],
    )
    def test_join_refused(self, members):
        data = {} if members is None else {"group_members": members}
        refusal, _ = check_ha_call("media_player.living_room_speaker", "join", data)
        assert refusal and "join" in refusal


# ---------------------------------------------------------------------------
# Legacy NLP path
# ---------------------------------------------------------------------------


class TestLegacyNlpPath:
    async def test_allowed_command_posts(self, client):
        c, fake = client
        result = await c.execute_command("turn on the kitchen")
        assert result.success
        assert fake.service_calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]

    @pytest.mark.parametrize(
        "text",
        [
            "unlock the front door",
            "open the garage door",
            "turn off helios plug",
            "turn off router power",
            "turn on robo",
        ],
    )
    async def test_refused_commands_never_reach_ha(self, client, text):
        c, fake = client
        result = await c.execute_command(text)
        assert not result.success
        assert fake.service_calls == []

    async def test_unlock_counted_with_guard_wording(self, client):
        from orchestrator.metrics import HA_CALL_REFUSED

        c, fake = client
        before = HA_CALL_REFUSED.labels(path="nlp")._value.get()
        result = await c.execute_command("unlock the front door")
        assert "confirmation step" in result.message
        assert HA_CALL_REFUSED.labels(path="nlp")._value.get() == before + 1
        assert fake.service_calls == []

    async def test_lock_still_works(self, client):
        c, fake = client
        result = await c.execute_command("lock the front door")
        assert result.success
        assert fake.service_calls == [("/api/services/lock/lock", {"entity_id": "lock.front_door"})]

    async def test_compound_command_refuses_only_denied_part(self, client):
        c, fake = client
        result = await c.execute_command("turn on the kitchen and turn off helios plug")
        assert not result.success
        assert fake.service_calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]

    def test_service_map_has_no_unlock_or_open(self):
        c = HomeAssistantClient(url="http://ha.test", token="t")
        assert c._get_service_for_action("unlock", "lock") is None
        assert c._get_service_for_action("open_cover", "cover") is None


# ---------------------------------------------------------------------------
# /api/ha/entities + /api/ha/command
# ---------------------------------------------------------------------------


class TestRoutes:
    async def test_entities_hides_denied_and_unlisted(self, client, monkeypatch):
        c, _ = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        out = await api_routes.list_ha_entities()
        ids = {e["entity_id"] for ents in out["controllable"].values() for e in ents}
        assert "light.kitchen" in ids and "media_player.living_room_speaker" in ids
        assert not ids & {PLUG, "switch.router_power", "media_player.blocked_speaker", "vacuum.robo"}
        assert "vacuum" not in out["controllable"]
        assert out["total"] == len(ids)

    async def test_structured_command_allowed(self, client, monkeypatch):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        out = await api_routes.execute_ha_command(
            HACommandRequest(entity_id="light.kitchen", service="turn_on", data={"brightness": 10, "entity_id": PLUG})
        )
        assert out["ok"] is True and "success" not in out
        assert fake.service_calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen", "brightness": 10})]

    @pytest.mark.parametrize(
        "entity_id,service",
        [(PLUG, "turn_off"), ("lock.front_door", "unlock"), ("vacuum.robo", "start"), ("light.kitchen", "x.y")],
    )
    async def test_structured_command_refused(self, client, monkeypatch, entity_id, service):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        resp = await api_routes.execute_ha_command(HACommandRequest(entity_id=entity_id, service=service))
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["ok"] is False and body["error"].startswith("Failed:")
        assert fake.service_calls == []

    async def test_structured_command_ha_failure_is_generic_502(self, client, monkeypatch):
        c, fake = client

        async def boom(*a, **k):
            from orchestrator.ha_integration import ExecutionResult

            return ExecutionResult(
                success=False, action="x", entity_id="light.kitchen", message="HTTP 500: secret body"
            )

        monkeypatch.setattr(c, "call_service", boom)
        monkeypatch.setattr(api_routes, "ha_client", c)
        resp = await api_routes.execute_ha_command(HACommandRequest(entity_id="light.kitchen", service="turn_on"))
        assert resp.status_code == 502
        assert json.loads(resp.body) == {"ok": False, "error": "Home Assistant call failed"}

    async def test_text_command_refused(self, client, monkeypatch):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        out = await api_routes.execute_ha_command(HACommandRequest(command="unlock the front door"))
        assert out["ok"] is False
        assert fake.service_calls == []

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"command": "turn on kitchen", "entity_id": "light.kitchen", "service": "turn_on"},
            {"entity_id": "light.kitchen"},
            {"service": "turn_on"},
            {"command": ""},
            {"entity_id": "light.kitchen", "service": "turn_on", "data": {f"k{i}": i for i in range(33)}},
        ],
    )
    def test_request_shape_validation(self, body):
        with pytest.raises(ValueError):
            HACommandRequest(**body)

    async def test_structured_refusal_counted_on_api_path(self, client, monkeypatch):
        from orchestrator.metrics import HA_CALL_REFUSED

        c, _ = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        before = HA_CALL_REFUSED.labels(path="api")._value.get()
        await api_routes.execute_ha_command(HACommandRequest(entity_id=PLUG, service="turn_off"))
        assert HA_CALL_REFUSED.labels(path="api")._value.get() == before + 1

    async def test_structured_play_media_arbitrary_url_refused(self, client, monkeypatch):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        resp = await api_routes.execute_ha_command(
            HACommandRequest(
                entity_id="media_player.living_room_speaker",
                service="play_media",
                data={"media_content_id": "http://10.0.0.1/admin", "media_content_type": "music"},
            )
        )
        assert resp.status_code == 403
        assert fake.service_calls == []

    async def test_text_command_allowed_shape(self, client, monkeypatch):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        out = await api_routes.execute_ha_command(HACommandRequest(command="turn on the kitchen"))
        assert out["ok"] is True
        assert set(out) == {"ok", "action", "entity_id", "message"}
        assert fake.service_calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]


# ---------------------------------------------------------------------------
# Chat tool path (tool_handlers.tool_home_assistant)
# ---------------------------------------------------------------------------


class TestToolPath:
    @pytest.fixture
    def tool_client(self, client, monkeypatch):
        from orchestrator import tool_handlers

        c, fake = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        return tool_handlers, fake

    async def test_refusal_counted_on_tool_path_only(self, tool_client):
        from orchestrator.metrics import HA_CALL_REFUSED

        th, fake = tool_client
        before = {p: HA_CALL_REFUSED.labels(path=p)._value.get() for p in ("tool", "nlp", "api")}
        out = await th.tool_home_assistant("lock.front_door", "unlock")
        assert "confirmation step" in out
        after = {p: HA_CALL_REFUSED.labels(path=p)._value.get() for p in ("tool", "nlp", "api")}
        assert after == {**before, "tool": before["tool"] + 1}
        assert fake.service_calls == []

    async def test_play_media_drops_bypass_keys_end_to_end(self, tool_client):
        th, fake = tool_client
        await th.tool_home_assistant(
            "media_player.living_room_speaker",
            "play_media",
            {
                "media_content_id": "media-source://radio_browser/abc",
                "media_content_type": "music",
                "media": {"media_content_id": "http://attacker/x"},
                "extra": {"thumb": "http://attacker/t.png"},
                "entity_id": PLUG,
            },
        )
        assert len(fake.service_calls) == 1
        path, body = fake.service_calls[0]
        assert path == "/api/services/media_player/play_media"
        assert body == {
            "entity_id": "media_player.living_room_speaker",
            "media_content_id": "media-source://radio_browser/abc",
            "media_content_type": "music",
        }

    async def test_play_media_arbitrary_url_never_reaches_ha(self, tool_client):
        th, fake = tool_client
        out = await th.tool_home_assistant(
            "media_player.living_room_speaker",
            "play_media",
            {"media_content_id": "https://evil.example/x.mp3", "media_content_type": "music"},
        )
        assert out.startswith("Failed:")
        assert fake.service_calls == []

    async def test_join_with_denied_member_never_reaches_ha(self, tool_client):
        th, fake = tool_client
        out = await th.tool_home_assistant(
            "media_player.living_room_speaker", "join", {"group_members": ["media_player.blocked_speaker"]}
        )
        assert out.startswith("Failed:")
        assert fake.service_calls == []


# ---------------------------------------------------------------------------
# Fast path (fast_path.try_fast_path) on top of the guarded NLP path
# ---------------------------------------------------------------------------


class TestFastPath:
    @pytest.mark.parametrize("text", ["turn off helios plug", "unlock the front door", "turn on robo"])
    async def test_guard_refusal_falls_through(self, client, text):
        from orchestrator.fast_path import is_fast_path_eligible, try_fast_path

        c, fake = client
        # Must actually get as far as the guard, not be filtered out earlier.
        assert is_fast_path_eligible(text) and c.parse_command(text)
        result = await try_fast_path(text, c)
        assert result.handled is False
        assert result.response_text == ""
        assert fake.service_calls == []

    async def test_allowed_command_handled(self, client):
        from orchestrator.fast_path import try_fast_path

        c, fake = client
        result = await try_fast_path("turn on the kitchen", c)
        assert result.handled is True
        assert fake.service_calls == [("/api/services/light/turn_on", {"entity_id": "light.kitchen"})]

    async def test_failure_log_does_not_carry_result_message(self, client, caplog):
        """The fall-through log must not include result.message (it can quote
        the user's text or the guard's refusal)."""
        import logging

        from orchestrator.fast_path import try_fast_path

        c, _ = client
        with caplog.at_level(logging.INFO, logger="orchestrator.fast_path"):
            result = await try_fast_path("turn on zzqxsecretthing", c)
        assert c.parse_command("turn on zzqxsecretthing")
        assert result.handled is False
        fell = [r.getMessage() for r in caplog.records if "falling through" in r.getMessage()]
        assert fell, "expected a fall-through log line"
        assert all("zzqxsecretthing" not in m and "Could not" not in m for m in fell)


# ---------------------------------------------------------------------------
# Device-setting entities (entity_category config / diagnostic)
# ---------------------------------------------------------------------------


class TestDeviceSettings:
    def test_registry_category_decides(self, registry):
        assert ha_guard.device_setting_reason("switch.porch_widget_toggle") == "registry"
        assert ha_guard.device_setting_reason("switch.desk_plug_status_report") == "registry"
        assert ha_guard.device_setting_reason("switch.office_desk") is None

    def test_name_match_applies_even_with_uncategorised_registry_entry(self, registry):
        # Many camera integrations ship their enable/motion switches with no
        # entity_category: the name check still refuses them.
        assert ha_guard.device_setting_reason("switch.porch_cam_power_save") == "name"

    @pytest.mark.parametrize(
        "entity_id",
        [
            "switch.porch_camera_enabled",
            "switch.porchcamera",
            "switch.cameras",
            "switch.cam1_power",
            "switch.frontcam",
            "switch.hall_motion_detection",
            "switch.yard_cam_privacy_mode",
            "switch.garage_recordings",
            "switch.plug_leds",
            "light.desk_plug_status_light",
            "switch.door_push_notifications",
            "switch.yard_ir_mode",
            "switch.doorbell_chime",
        ],
    )
    def test_name_match(self, entity_id):
        assert ha_device_setting(entity_id)

    @pytest.mark.parametrize(
        "entity_id",
        [
            "switch.office_desk",
            "light.kitchen",
            "switch.porch_flood",
            "fan.bedroom",
            "light.led_strip",
            "media_player.record_player",
        ],
    )
    def test_name_match_leaves_ordinary_entities(self, entity_id):
        assert not ha_device_setting(entity_id)

    def test_scene_or_group_with_device_setting_member(self, monkeypatch, registry):
        from orchestrator.ha_integration import Entity

        ents = {
            "scene.away": Entity(
                "scene.away", "scene", "Away", "x", {"entity_id": ["light.kitchen", "switch.porch_widget_toggle"]}
            ),
            "scene.evening": Entity("scene.evening", "scene", "Evening", "x", {"entity_id": ["light.kitchen"]}),
        }

        class _Client:
            def get_entity(self, entity_id):
                return ents.get(entity_id)

        monkeypatch.setattr(shared, "ha_client", _Client())
        assert ha_guard.device_setting_reason("scene.away") == "member"
        assert ha_guard.device_setting_reason("scene.evening") is None
        assert check_ha_call("scene.away", "turn_on")[0]
        assert check_ha_call("scene.evening", "turn_on")[0] is None

    def test_guard_refuses_by_default_allows_for_dashboard(self, registry):
        refusal, _ = check_ha_call("switch.porch_widget_toggle", "turn_off")
        assert refusal and ha_guard.DEVICE_SETTING_REFUSAL in refusal and "Nothing was changed" in refusal
        assert "dashboard" not in refusal.lower()
        refusal, data = check_ha_call("switch.porch_widget_toggle", "turn_off", {}, allow_device_settings=True)
        assert refusal is None and data == {}

    def test_dashboard_flag_allows_name_matched_entity(self):
        assert check_ha_call("switch.porch_camera_enabled", "turn_on", allow_device_settings=True)[0] is None

    def test_registry_change_invalidates_tool_schema(self, monkeypatch):
        import asyncio

        async def fetch(url, token):
            return dict(REGISTRY)

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        monkeypatch.setattr(shared, "_ha_tool_cache", {"stale": True})
        asyncio.get_event_loop().run_until_complete(ha_registry.get_registry_meta("http://ha.test:8123", "t"))
        assert shared._ha_tool_cache is None

    def test_dashboard_flag_does_not_bypass_other_checks(self, registry):
        assert check_ha_call(PLUG, "turn_off", allow_device_settings=True)[0]
        assert check_ha_call("lock.front_door", "unlock", allow_device_settings=True)[0]

    async def test_tool_path_refuses_and_never_reaches_ha(self, client, registry, monkeypatch):
        from orchestrator import tool_handlers
        from orchestrator.metrics import HA_CALL_REFUSED, HA_DEVICE_SETTING_REFUSED

        c, fake = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        before = HA_CALL_REFUSED.labels(path="tool")._value.get()
        before_src = HA_DEVICE_SETTING_REFUSED.labels(path="tool", source="registry")._value.get()
        out = await tool_handlers.tool_home_assistant("switch.porch_widget_toggle", "turn_off")
        assert ha_guard.DEVICE_SETTING_REFUSAL in out
        assert fake.service_calls == []
        assert HA_CALL_REFUSED.labels(path="tool")._value.get() == before + 1
        assert HA_DEVICE_SETTING_REFUSED.labels(path="tool", source="registry")._value.get() == before_src + 1

    async def test_other_refusals_not_counted_as_device_setting(self, client, monkeypatch):
        from orchestrator import tool_handlers
        from orchestrator.metrics import HA_DEVICE_SETTING_REFUSED

        c, _ = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        before = {s: HA_DEVICE_SETTING_REFUSED.labels(path="tool", source=s)._value.get() for s in ("registry", "name")}
        # Denied by name pattern *and* the deny list: the deny list fires first.
        monkeypatch.setattr(shared, "HA_TOOL_DENY_ENTITIES", "switch.porch_cam_*")
        await tool_handlers.tool_home_assistant("switch.porch_cam_power_save", "turn_off")
        after = {s: HA_DEVICE_SETTING_REFUSED.labels(path="tool", source=s)._value.get() for s in ("registry", "name")}
        assert after == before

    async def test_refusal_log_only_for_known_entities(self, client, monkeypatch, caplog):
        import logging

        from orchestrator import tool_handlers

        c, _ = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        with caplog.at_level(logging.WARNING, logger="orchestrator.tool_handlers"):
            await tool_handlers.tool_home_assistant("switch.say_hello_world_camera", "turn_off")
            await tool_handlers.tool_home_assistant("switch.porch_cam_power_save", "turn_off")
        msgs = [r.getMessage() for r in caplog.records]
        assert not any("say_hello_world" in m for m in msgs)
        assert any("switch.porch_cam_power_save" in m and "source=name" in m for m in msgs)

    async def test_tool_path_registry_down_uses_name_match(self, client, monkeypatch):
        from orchestrator import tool_handlers

        c, fake = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        out = await tool_handlers.tool_home_assistant("switch.porch_cam_power_save", "turn_off")
        assert ha_guard.DEVICE_SETTING_REFUSAL in out
        assert fake.service_calls == []
        await tool_handlers.tool_home_assistant("switch.office_desk", "turn_off")
        assert fake.service_calls == [("/api/services/switch/turn_off", {"entity_id": "switch.office_desk"})]

    async def test_tool_unknown_id_suggestions_skip_device_settings(self, client, registry, monkeypatch):
        from orchestrator import tool_handlers

        c, fake = client
        monkeypatch.setattr(tool_handlers, "ha_client", c)
        out = await tool_handlers.tool_home_assistant("switch.porch_widget", "turn_off")
        assert "switch.porch_widget_toggle" not in out
        assert fake.service_calls == []

    async def test_nlp_path_refuses(self, client, registry):
        from orchestrator.metrics import HA_CALL_REFUSED, HA_DEVICE_SETTING_REFUSED

        c, fake = client
        before = HA_CALL_REFUSED.labels(path="nlp")._value.get()
        before_src = HA_DEVICE_SETTING_REFUSED.labels(path="nlp", source="registry")._value.get()
        result = await c.execute_command("turn off porch widget toggle")
        assert result.success is False and ha_guard.DEVICE_SETTING_REFUSAL in result.message
        assert fake.service_calls == []
        assert HA_CALL_REFUSED.labels(path="nlp")._value.get() == before + 1
        assert HA_DEVICE_SETTING_REFUSED.labels(path="nlp", source="registry")._value.get() == before_src + 1

    async def test_fast_path_falls_through(self, client, registry):
        from orchestrator.fast_path import try_fast_path

        c, fake = client
        result = await try_fast_path("turn off porch widget toggle", c)
        assert result.handled is False
        assert fake.service_calls == []

    async def test_structured_api_call_allowed(self, client, registry, monkeypatch):
        c, fake = client
        monkeypatch.setattr(api_routes, "ha_client", c)
        await api_routes.execute_ha_command(
            HACommandRequest(entity_id="switch.porch_widget_toggle", service="turn_off")
        )
        assert fake.service_calls == [("/api/services/switch/turn_off", {"entity_id": "switch.porch_widget_toggle"})]

    def test_tool_schema_listing_hides_device_settings(self, client, registry):
        from orchestrator.tool_definitions import format_ha_entity_listing

        c, _ = client
        listing = format_ha_entity_listing({d: c.get_entities_by_domain(d) for d in ha_guard.HA_LISTED_DOMAINS})
        assert "switch.office_desk" in listing and "switch.porch_flood" in listing
        assert "switch.porch_widget_toggle" not in listing
        assert "switch.porch_cam_power_save" not in listing


class TestEnsureFresh:
    """ha_registry.ensure_fresh: a chat/voice turn never waits on a refresh
    when a cache exists, and a slow HA still backs off and is counted."""

    async def test_warm_cache_returns_without_waiting(self, monkeypatch):
        import asyncio
        import time

        started = asyncio.Event()

        async def slow_fetch(url, token):
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(ha_registry, "_fetch", slow_fetch)
        monkeypatch.setattr(ha_registry, "_cache", {"light.kitchen": {"category": None}})
        t0 = time.monotonic()
        await ha_registry.ensure_fresh("http://ha.test:8123", "t")
        assert time.monotonic() - t0 < 0.5
        await asyncio.wait_for(started.wait(), 1)
        ha_registry._refresh_task.cancel()

    async def test_cold_cache_waits_capped_and_slow_fetch_backs_off(self, monkeypatch):
        import asyncio
        import time

        from orchestrator.metrics import HA_REGISTRY_FETCH

        async def slow_fetch(url, token):
            await asyncio.sleep(30)

        monkeypatch.setattr(ha_registry, "_fetch", slow_fetch)
        monkeypatch.setattr(ha_registry, "FETCH_TIMEOUT_SECONDS", 0.3)
        before = HA_REGISTRY_FETCH.labels(result="error")._value.get()
        t0 = time.monotonic()
        await ha_registry.ensure_fresh("http://ha.test:8123", "t", wait_seconds=0.1)
        assert time.monotonic() - t0 < 0.25
        # The shielded fetch runs on to its own timeout and records the failure.
        await asyncio.wait_for(asyncio.shield(ha_registry._refresh_task), 2)
        assert ha_registry._failed_at > 0
        assert HA_REGISTRY_FETCH.labels(result="error")._value.get() == before + 1

    async def test_success_sets_entity_gauge(self, monkeypatch):
        from orchestrator.metrics import HA_REGISTRY_ENTITIES

        async def fetch(url, token):
            return dict(REGISTRY)

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        await ha_registry.ensure_fresh("http://ha.test:8123", "t")
        assert ha_registry.cached_meta() == REGISTRY
        assert HA_REGISTRY_ENTITIES._value.get() == len(REGISTRY)
