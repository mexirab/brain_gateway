"""Tests for the dashboard /home enrichment and the registry the HA guard reads:

- ``ha_registry.get_registry_meta`` — WS registry read → {entity: area/category/hidden},
  device-area inheritance, caching, never raises, metrics;
- ``ha_registry.ensure_fresh`` — a chat/voice turn never waits on a refresh when
  a cache exists; a slow HA still backs off and is counted;
- ``GET /api/ha/entities`` — area/category/brightness/volume fields, entities
  hidden in HA left out, ``rooms_available``;
- ``HomeAssistantClient.refresh_entities`` — shared fetch, back-off, cache TTL,
  ``last_refresh_ok``.
"""

import asyncio
import json
import time
from datetime import timedelta

import pytest
import websockets

from orchestrator import api_routes, ha_guard, ha_registry, shared
from orchestrator.ha_integration import Entity
from orchestrator.metrics import HA_REGISTRY_ENTITIES, HA_REGISTRY_FETCH
from orchestrator.schemas import HACommandRequest
from orchestrator.tests._helpers import metric_value

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
        before = metric_value(HA_REGISTRY_FETCH, result="error")
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == {}
        assert metric_value(HA_REGISTRY_FETCH, result="error") == before + 1

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
async def ha(ha_voice_ha_client, monkeypatch):
    monkeypatch.setattr(shared, "HA_TOOL_DENY_ENTITIES", "")
    monkeypatch.setattr(shared, "HELIOS_PLUG_ENTITY", "switch.helios_plug")
    c, _ = await ha_voice_ha_client(STATES, populate=False)
    monkeypatch.setattr(api_routes, "ha_client", c)
    return c


@pytest.fixture
async def counting_client(ha_voice_ha_client):
    """A client over STATES that hasn't fetched yet; ``fake.calls`` counts fetches,
    ``fake.status = 500`` makes HA fail."""
    return await ha_voice_ha_client(STATES, populate=False)


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
        resp = await api_routes.execute_ha_command(HACommandRequest(entity_id="light.not_there", service="turn_on"))
        assert resp.status_code == 404
        assert json.loads(resp.body) == {"ok": False, "error": "Unknown entity"}


class TestRefreshResilience:
    async def test_concurrent_refreshes_share_one_fetch(self, counting_client):
        c, fake = counting_client
        fake.delay = 0.05  # all eight start while the first fetch is in flight
        await asyncio.gather(*(c.refresh_entities(max_age_seconds=3) for _ in range(8)))
        assert fake.calls == 1

    async def test_failure_backs_off(self, counting_client):
        c, fake = counting_client
        fake.status = 500
        await c.refresh_entities(max_age_seconds=3)
        await c.refresh_entities(max_age_seconds=3)
        assert fake.calls == 1 and c.last_refresh_ok is False
        await c.refresh_entities(force=True)  # forced refresh ignores the back-off
        assert fake.calls == 2

    async def test_entities_reports_stale_state(self, counting_client, monkeypatch):
        c, fake = counting_client
        await c.refresh_entities(force=True)
        fake.status = 500
        c._last_refresh -= timedelta(seconds=10)
        monkeypatch.setattr(api_routes, "ha_client", c)
        _patch_ws(monkeypatch, FakeWS)
        out = await api_routes.list_ha_entities()
        assert out["state_ok"] is False and out["total"] > 0  # last known states still served

    async def test_unknown_entity_with_ha_down_is_502(self, counting_client, monkeypatch):
        c, fake = counting_client
        fake.status = 500
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


def _ent(entity_id, attrs, state="on"):
    return Entity.from_ha_state({"entity_id": entity_id, "state": state, "attributes": attrs})


_LIGHT_BRIGHTNESS = {"supported_color_modes": ["brightness"]}


class TestEntityViewEdgeCases:
    @pytest.mark.parametrize(
        "entity_id, attrs, state, expected",
        [
            pytest.param(
                "light.a", {"brightness": 200}, "on", {"supports_brightness": True, "brightness": 200}, id="no-modes"
            ),
            pytest.param(
                "light.a", {}, "off", {"supports_brightness": False, "brightness": None, "is_group": False}, id="onoff"
            ),
            pytest.param(
                "light.a",
                {"supported_color_modes": "brightness", "brightness": 5},
                "on",
                {"supports_brightness": True},
                id="modes-str-with-brightness",
            ),
            pytest.param(
                "light.b", {"supported_color_modes": "brightness"}, "on", {"supports_brightness": False}, id="modes-str"
            ),
            pytest.param(
                "light.a",
                {"supported_color_modes": ["onoff"], "brightness": 255},
                "on",
                {"supports_brightness": False},
                id="onoff-stray-brightness",
            ),
            pytest.param(
                "light.a",
                {**_LIGHT_BRIGHTNESS, "brightness": None},
                "off",
                {"brightness": None, "supports_brightness": True},
                id="off-light",
            ),
            pytest.param(
                "light.b",
                {"supported_color_modes": ["xy"], "brightness": "128"},
                "on",
                {"brightness": None},
                id="non-int-brightness",
            ),
            pytest.param("light.a", {"entity_id": "light.b"}, "on", {"is_group": False}, id="group-needs-list"),
            pytest.param(
                "media_player.a",
                {"volume_level": "loud", "media_title": 42},
                "on",
                {"volume": None, "media_title": None},
                id="media-bad-attrs",
            ),
            pytest.param("media_player.b", {}, "off", {"volume": None, "media_title": None}, id="media-no-attrs"),
            pytest.param("media_player.a", {"volume_level": 0}, "on", {"volume": 0}, id="volume-zero-kept"),
            pytest.param(
                "climate.a",
                {"current_temperature": "warm"},
                "off",
                {"current_temperature": None, "temperature": None},
                id="climate-bad-temps",
            ),
        ],
    )
    def test_view_fields(self, entity_id, attrs, state, expected):
        out = ha_registry.entity_view(_ent(entity_id, attrs, state=state), {})
        assert {k: out[k] for k in expected} == expected

    def test_other_domains_get_only_base_fields(self):
        out = ha_registry.entity_view(_ent("switch.a", {"brightness": 10, "volume_level": 0.5}), {"area": "Room A"})
        assert set(out) == {"entity_id", "friendly_name", "state", "area", "category"}
        assert out["area"] == "Room A" and out["category"] is None

    def test_none_attributes_do_not_crash(self):
        e = Entity(entity_id="light.a", domain="light", friendly_name="A", state="on", attributes=None)
        out = ha_registry.entity_view(e, {})
        assert out["supports_brightness"] is False and out["brightness"] is None


class TestRegistryRecovery:
    async def test_success_after_backoff_expires_replaces_cache(self, monkeypatch):
        def boom():
            raise OSError("refused")

        _patch_ws(monkeypatch, boom)
        assert await ha_registry.get_registry_meta("http://ha.test:8123", "tok") == {}
        monkeypatch.setattr(ha_registry, "_failed_at", time.monotonic() - ha_registry.FAILURE_RETRY_SECONDS - 1)

        calls = _patch_ws(monkeypatch, FakeWS)
        before = metric_value(HA_REGISTRY_FETCH, result="ok")
        meta = await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        assert len(calls) == 1 and meta["light.desk"]["area"] == "Office"
        assert ha_registry._failed_at == 0.0
        assert metric_value(HA_REGISTRY_FETCH, result="ok") == before + 1

    async def test_cache_expiry_triggers_refetch(self, monkeypatch):
        calls = _patch_ws(monkeypatch, FakeWS)
        await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        monkeypatch.setattr(ha_registry, "_fetched_at", time.monotonic() - ha_registry.REGISTRY_TTL_SECONDS - 1)
        await ha_registry.get_registry_meta("http://ha.test:8123", "tok")
        assert len(calls) == 2

    async def test_non_http_url_never_connects(self, monkeypatch):
        calls = _patch_ws(monkeypatch, FakeWS)
        assert await ha_registry.get_registry_meta("ha.test:8123", "tok") == {}
        assert await ha_registry.get_registry_meta("", "tok") == {}
        assert calls == []


class TestRefreshCacheSemantics:
    async def test_default_path_honours_cache_ttl(self, counting_client):
        """Chat callers (no max_age) still get the 5-min cache, not a fetch per turn."""
        c, fake = counting_client
        await c.refresh_entities()
        c._last_refresh -= timedelta(seconds=60)
        await c.refresh_entities()
        assert fake.calls == 1
        c._last_refresh -= c.cache_ttl
        await c.refresh_entities()
        assert fake.calls == 2

    async def test_max_age_overrides_ttl(self, counting_client):
        c, fake = counting_client
        await c.refresh_entities()
        c._last_refresh -= timedelta(seconds=10)
        await c.refresh_entities(max_age_seconds=5)
        assert fake.calls == 2

    async def test_successful_refresh_clears_backoff(self, counting_client):
        c, fake = counting_client
        assert c.last_refresh_ok is True  # before any fetch
        fake.status = 500
        assert await c.refresh_entities(max_age_seconds=3) == 0
        assert c._last_failure is not None and c.last_refresh_ok is False
        fake.status = 200
        n = await c.refresh_entities(force=True)
        assert n == len(STATES)
        assert c._last_failure is None and c.last_refresh_ok is True
        assert c.has_entities()

    async def test_backoff_expires(self, counting_client):
        c, fake = counting_client
        fake.status = 500
        await c.refresh_entities(max_age_seconds=3)
        c._last_failure -= c.FAILURE_BACKOFF + timedelta(seconds=1)
        fake.status = 200
        await c.refresh_entities(max_age_seconds=3)
        assert fake.calls == 2 and c.last_refresh_ok is True

    @pytest.mark.parametrize("failure", ["http-500", "connect-error"])
    async def test_failure_keeps_cached_entities(self, counting_client, failure):
        c, fake = counting_client
        n = await c.refresh_entities()
        if failure == "http-500":
            fake.status = 500
        else:
            fake.fail = True
        assert await c.refresh_entities(force=True) == n
        assert c.last_refresh_ok is False  # the flag flips; the old cache stays
        assert c.get_entity("light.desk") is not None


# What get_registry_meta returns for the HA guard's category check.
GUARD_META = {
    "light.kitchen": {"area": "Kitchen", "category": None, "hidden": False},
    "switch.porch_widget_toggle": {"area": "Porch", "category": "config", "hidden": False},
}


class TestEnsureFresh:
    """ha_registry.ensure_fresh: a chat/voice turn never waits on a refresh
    when a cache exists, and a slow HA still backs off and is counted."""

    async def test_warm_cache_returns_without_waiting(self, monkeypatch):
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
        async def slow_fetch(url, token):
            await asyncio.sleep(30)

        monkeypatch.setattr(ha_registry, "_fetch", slow_fetch)
        monkeypatch.setattr(ha_registry, "FETCH_TIMEOUT_SECONDS", 0.3)
        before = metric_value(HA_REGISTRY_FETCH, result="error")
        t0 = time.monotonic()
        await ha_registry.ensure_fresh("http://ha.test:8123", "t", wait_seconds=0.1)
        assert time.monotonic() - t0 < 0.25
        # The shielded fetch runs on to its own timeout and records the failure.
        await asyncio.wait_for(asyncio.shield(ha_registry._refresh_task), 2)
        assert ha_registry._failed_at > 0
        assert metric_value(HA_REGISTRY_FETCH, result="error") == before + 1

    async def test_success_sets_entity_gauge(self, monkeypatch):
        async def fetch(url, token):
            return dict(GUARD_META)

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        await ha_registry.ensure_fresh("http://ha.test:8123", "t")
        assert ha_registry.cached_meta() == GUARD_META
        assert metric_value(HA_REGISTRY_ENTITIES) == len(GUARD_META)

    async def test_concurrent_calls_start_one_fetch(self, monkeypatch):
        calls = 0
        release = asyncio.Event()

        async def fetch(url, token):
            nonlocal calls
            calls += 1
            await release.wait()
            return dict(GUARD_META)

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        waiters = [
            asyncio.create_task(ha_registry.ensure_fresh("http://ha.test:8123", "t", wait_seconds=2)) for _ in range(3)
        ]
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(*waiters)
        assert calls == 1
        assert ha_registry.cached_meta() == GUARD_META

    async def test_failed_refresh_keeps_last_good_cache(self, monkeypatch):
        async def boom(url, token):
            raise RuntimeError("down")

        monkeypatch.setattr(ha_registry, "_fetch", boom)
        monkeypatch.setattr(ha_registry, "_cache", dict(GUARD_META))
        await ha_registry.ensure_fresh("http://ha.test:8123", "t")
        await ha_registry._refresh_task
        assert ha_registry._failed_at > 0
        # The guard keeps refusing from the last good registry read.
        assert ha_guard.device_setting_reason("switch.porch_widget_toggle") == "registry"

    @pytest.mark.parametrize("url,token", [("", "t"), ("ha.test:8123", "t"), ("http://ha.test:8123", "")])
    async def test_unconfigured_never_fetches_or_raises(self, monkeypatch, url, token):
        async def fetch(url, token):
            raise AssertionError("must not fetch")

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        await ha_registry.ensure_fresh(url, token, wait_seconds=0.5)
        await ha_registry._refresh_task
        assert ha_registry.cached_meta() == {}

    async def test_unchanged_registry_keeps_tool_schema_cache(self, monkeypatch):
        async def fetch(url, token):
            return dict(GUARD_META)

        monkeypatch.setattr(ha_registry, "_fetch", fetch)
        monkeypatch.setattr(ha_registry, "_cache", dict(GUARD_META))
        monkeypatch.setattr(shared, "_ha_tool_cache", {"warm": True})
        await ha_registry.get_registry_meta("http://ha.test:8123", "t")
        assert ha_registry._fetched_at > 0
        assert shared._ha_tool_cache == {"warm": True}
