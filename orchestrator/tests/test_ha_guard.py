"""Shared Home Assistant guard (ha_guard.check_ha_call) and the paths that
must use it besides the chat tool:

- ``check_ha_call`` — domain, deny list, service allow-list, targeting keys,
  play_media / join argument checks;
- the legacy NLP path ``HomeAssistantClient.execute_command`` (fast path and
  ``POST /api/ha/command`` text) — refused calls never reach HA;
- ``GET /api/ha/entities`` hides denied + unlisted-domain entities;
- ``POST /api/ha/command`` structured body is guarded.
"""

import json

import httpx
import pytest

from orchestrator import api_routes, ha_guard, shared
from orchestrator.ha_guard import check_ha_call
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
]


@pytest.fixture(autouse=True)
def _deny_defaults(monkeypatch):
    monkeypatch.setattr(shared, "HA_TOOL_DENY_ENTITIES", "switch.router_*,media_player.blocked_speaker")
    monkeypatch.setattr(shared, "HELIOS_PLUG_ENTITY", PLUG)
    monkeypatch.setattr(shared, "FOCUS_AUDIO_LOFI_URL", "https://stream.example/lofi.mp3", raising=False)
    monkeypatch.setattr(shared, "FOCUS_AUDIO_COFFEE_URL", "", raising=False)
    monkeypatch.setattr(ha_guard.settings, "orchestrator_url", "http://10.0.0.248:8888")


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
            "media-source://tts/cloud?message=hi",
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
        assert out["ok"] is True and out["success"] is True
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
        assert json.loads(resp.body)["ok"] is False
        assert fake.service_calls == []

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
        ],
    )
    def test_request_shape_validation(self, body):
        with pytest.raises(ValueError):
            HACommandRequest(**body)
