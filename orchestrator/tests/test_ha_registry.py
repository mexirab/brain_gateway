"""Tests for the dashboard /home enrichment:

- ``ha_registry.get_registry_meta`` — WS registry read → {entity: area/category/hidden},
  device-area inheritance, caching, never raises, metrics;
- ``GET /api/ha/entities`` — area/category/brightness/volume fields, entities
  hidden in HA left out, ``rooms_available``.
"""

import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest

from orchestrator import api_routes, ha_registry, shared
from orchestrator.ha_integration import HomeAssistantClient
from orchestrator.metrics import HA_REGISTRY_FETCH

ENTITY_REG = {
    "entity_categories": {"0": "config", "1": "diagnostic"},
    "entities": [
        {"ei": "light.desk", "di": "dev1"},  # area from its device
        {"ei": "light.lamp", "di": "dev1", "ai": "den"},  # own area override
        {"ei": "switch.cam_enabled", "di": "dev2", "ec": 0},
        {"ei": "scene.hidden_one", "hb": True},
        {"ei": "light.loose"},
        "garbage",
    ],
}
DEVICES = [{"id": "dev1", "area_id": "office"}, {"id": "dev2", "area_id": None}]
AREAS = [{"area_id": "office", "name": "Office"}, {"area_id": "den", "name": "Den"}]


class FakeWS:
    def __init__(self, auth_ok=True, fail_cmd=False):
        self.auth_ok = auth_ok
        self.fail_cmd = fail_cmd
        self.sent = []
        self.outbox = [{"type": "auth_required"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "auth":
            self.outbox.append({"type": "auth_ok" if self.auth_ok else "auth_invalid"})
            return
        result = {
            "config/entity_registry/list_for_display": ENTITY_REG,
            "config/device_registry/list": DEVICES,
            "config/area_registry/list": AREAS,
        }[msg["type"]]
        # An unrelated event first: the reader must skip it.
        self.outbox.append({"type": "event", "id": 99})
        self.outbox.append({"type": "result", "id": msg["id"], "success": not self.fail_cmd, "result": result})

    async def recv(self):
        return json.dumps(self.outbox.pop(0))


@pytest.fixture(autouse=True)
def _reset():
    ha_registry._reset_cache_for_tests()
    yield
    ha_registry._reset_cache_for_tests()


def _patch_ws(monkeypatch, ws_factory):
    import websockets

    calls = []

    def connect(url, **kw):
        calls.append(url)
        return ws_factory()

    monkeypatch.setattr(websockets, "connect", connect)
    return calls


class TestRegistry:
    async def test_parses_area_category_hidden(self, monkeypatch):
        calls = _patch_ws(monkeypatch, FakeWS)
        meta = await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        assert calls == ["ws://ha.test:8123/api/websocket"]
        assert meta["light.desk"] == {"area": "Office", "category": None, "hidden": False}
        assert meta["light.lamp"]["area"] == "Den"
        assert meta["switch.cam_enabled"] == {"area": None, "category": "config", "hidden": False}
        assert meta["scene.hidden_one"]["hidden"] is True
        assert meta["light.loose"]["area"] is None

    async def test_https_becomes_wss(self):
        assert ha_registry._ws_url("https://ha.example/") == "wss://ha.example/api/websocket"
        assert ha_registry._ws_url("ftp://x") is None

    async def test_cached_between_calls(self, monkeypatch):
        calls = _patch_ws(monkeypatch, FakeWS)
        await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        assert len(calls) == 1

    @pytest.mark.parametrize("ws", [lambda: FakeWS(auth_ok=False), lambda: FakeWS(fail_cmd=True)])
    async def test_failure_returns_empty_and_counts(self, monkeypatch, ws):
        _patch_ws(monkeypatch, ws)
        before = HA_REGISTRY_FETCH.labels(result="error")._value.get()
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == {}
        assert HA_REGISTRY_FETCH.labels(result="error")._value.get() == before + 1

    async def test_failure_keeps_last_good_and_backs_off(self, monkeypatch):
        _patch_ws(monkeypatch, FakeWS)
        good = await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        monkeypatch.setattr(ha_registry, "_fetched_at", time.monotonic() - ha_registry.REGISTRY_TTL_SECONDS - 1)

        def boom():
            raise OSError("refused")

        calls = _patch_ws(monkeypatch, boom)
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == good
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == good
        assert len(calls) == 1  # second call inside the retry back-off

    async def test_no_token_no_connect(self, monkeypatch):
        calls = _patch_ws(monkeypatch, FakeWS)
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "") == {}
        assert calls == []


STATES = [
    {
        "entity_id": "light.desk",
        "state": "on",
        "attributes": {"friendly_name": "Desk", "brightness": 128, "supported_color_modes": ["color_temp", "xy"]},
    },
    {
        "entity_id": "light.plain",
        "state": "off",
        "attributes": {"friendly_name": "Plain", "supported_color_modes": ["onoff"]},
    },
    {
        "entity_id": "light.room_group",
        "state": "off",
        "attributes": {"friendly_name": "Room", "entity_id": ["light.desk"], "supported_color_modes": ["brightness"]},
    },
    {
        "entity_id": "media_player.speaker",
        "state": "playing",
        "attributes": {"friendly_name": "Speaker", "volume_level": 0.4, "media_title": "x" * 300},
    },
    {
        "entity_id": "climate.thermostat",
        "state": "heat",
        "attributes": {"friendly_name": "Thermostat", "current_temperature": 70, "temperature": 68},
    },
    {"entity_id": "switch.cam_enabled", "state": "on", "attributes": {"friendly_name": "Cam"}},
    {"entity_id": "scene.hidden_one", "state": "unknown", "attributes": {"friendly_name": "Hidden"}},
    {"entity_id": "switch.helios_plug", "state": "on", "attributes": {"friendly_name": "Plug"}},
]


@pytest.fixture
async def ha(monkeypatch):
    monkeypatch.setattr(shared, "HA_TOOL_DENY_ENTITIES", "")
    monkeypatch.setattr(shared, "HELIOS_PLUG_ENTITY", "switch.helios_plug")
    c = HomeAssistantClient(
        url="http://ha.test:8123",
        token="t",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=STATES))),
    )
    monkeypatch.setattr(api_routes, "ha_client", c)
    yield c
    await c._http.aclose()


class TestEntitiesRoute:
    async def test_enriched_fields(self, ha, monkeypatch):
        _patch_ws(monkeypatch, FakeWS)
        out = await api_routes.list_ha_entities()
        by_id = {e["entity_id"]: e for ents in out["controllable"].values() for e in ents}

        assert out["rooms_available"] is True
        assert "scene.hidden_one" not in by_id  # hidden in HA
        assert "switch.helios_plug" not in by_id  # denied
        assert out["total"] == len(by_id)

        desk = by_id["light.desk"]
        assert desk["area"] == "Office" and desk["category"] is None
        assert desk["brightness"] == 128 and desk["supports_brightness"] is True and desk["is_group"] is False
        assert by_id["light.plain"]["supports_brightness"] is False
        assert by_id["light.room_group"]["is_group"] is True
        assert by_id["switch.cam_enabled"]["category"] == "config"

        speaker = by_id["media_player.speaker"]
        assert speaker["volume"] == 0.4 and len(speaker["media_title"]) == 120
        assert by_id["climate.thermostat"]["temperature"] == 68
        assert by_id["climate.thermostat"]["current_temperature"] == 70

    async def test_registry_down_still_lists(self, ha, monkeypatch):
        def boom():
            raise OSError("refused")

        _patch_ws(monkeypatch, boom)
        out = await api_routes.list_ha_entities()
        assert out["rooms_available"] is False
        ids = {e["entity_id"] for ents in out["controllable"].values() for e in ents}
        assert "light.desk" in ids and "scene.hidden_one" in ids
        assert all(e["area"] is None for ents in out["controllable"].values() for e in ents)

    async def test_state_is_fresh_not_five_minutes_old(self, ha, monkeypatch):
        """The page must see a toggle on reload: max_age 3 s, not the 5 min TTL."""
        _patch_ws(monkeypatch, FakeWS)
        await api_routes.list_ha_entities()
        first = ha._last_refresh
        ha._last_refresh = first - timedelta(seconds=10)
        await api_routes.list_ha_entities()
        assert ha._last_refresh > first


class TestCommandUnknownEntity:
    async def test_unknown_entity_is_404_not_false_success(self, ha):
        from orchestrator.schemas import HACommandRequest

        resp = await api_routes.execute_ha_command(HACommandRequest(entity_id="light.not_there", service="turn_on"))
        assert resp.status_code == 404
        assert json.loads(resp.body) == {"ok": False, "error": "Unknown entity"}


class TestRefreshResilience:
    @pytest.fixture
    def counting_client(self):
        state = {"calls": 0, "fail": False}

        async def handler(request):
            state["calls"] += 1
            await asyncio.sleep(0.05)
            if state["fail"]:
                return httpx.Response(500)
            return httpx.Response(200, json=STATES)

        c = HomeAssistantClient(
            url="http://ha.test:8123",
            token="t",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return c, state

    async def test_concurrent_refreshes_share_one_fetch(self, counting_client):
        c, state = counting_client
        await asyncio.gather(*(c.refresh_entities(max_age_seconds=3) for _ in range(8)))
        assert state["calls"] == 1

    async def test_failure_backs_off(self, counting_client):
        c, state = counting_client
        state["fail"] = True
        await c.refresh_entities(max_age_seconds=3)
        await c.refresh_entities(max_age_seconds=3)
        assert state["calls"] == 1 and c.last_refresh_ok is False
        await c.refresh_entities(force=True)  # forced refresh ignores the back-off
        assert state["calls"] == 2

    async def test_entities_reports_stale_state(self, counting_client, monkeypatch):
        c, state = counting_client
        await c.refresh_entities(force=True)
        state["fail"] = True
        c._last_refresh -= timedelta(seconds=10)
        monkeypatch.setattr(api_routes, "ha_client", c)
        _patch_ws(monkeypatch, FakeWS)
        out = await api_routes.list_ha_entities()
        assert out["state_ok"] is False and out["total"] > 0  # last known states still served

    async def test_unknown_entity_with_ha_down_is_502(self, counting_client, monkeypatch):
        from orchestrator.schemas import HACommandRequest

        c, state = counting_client
        state["fail"] = True
        monkeypatch.setattr(api_routes, "ha_client", c)
        resp = await api_routes.execute_ha_command(HACommandRequest(entity_id="light.not_there", service="turn_on"))
        assert resp.status_code == 502


class TestRegistryFailureConcurrency:
    async def test_waiters_on_the_lock_respect_the_backoff(self, monkeypatch):
        calls = []

        class SlowFail:
            async def __aenter__(self):
                calls.append(1)
                await asyncio.sleep(0.05)
                raise OSError("down")

            async def __aexit__(self, *a):
                return False

        import websockets

        monkeypatch.setattr(websockets, "connect", lambda url, **kw: SlowFail())
        out = await asyncio.gather(*(ha_registry.get_registry_meta("http://ha.test:8123", "tok") for _ in range(5)))
        assert out == [{}] * 5
        assert len(calls) == 1

    async def test_hung_ha_is_bounded_by_the_timeout(self, monkeypatch):
        class Hang(FakeWS):
            async def recv(self):
                await asyncio.sleep(3600)

        _patch_ws(monkeypatch, Hang)
        monkeypatch.setattr(ha_registry, "FETCH_TIMEOUT_SECONDS", 0.1)
        t0 = time.monotonic()
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == {}
        assert time.monotonic() - t0 < 2
