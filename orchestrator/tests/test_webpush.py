"""Tests for self-hosted Web Push (webpush_manager + routes_webpush).

No network: sends are exercised with `_send_one` monkeypatched, plus a
pywebpush smoke where `webpush` (or the HTTP POST under it) is faked.
"""

import asyncio
import base64
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from orchestrator import shared, state_store, webpush_manager  # noqa: F401
from orchestrator.config import settings

APPLE = "https://web.push.apple.com/abc"
FCM = "https://fcm.googleapis.com/fcm/send/x"
MOZ = "https://updates.push.services.mozilla.com/wpush/v2/x"

P256DH = "B" + "A" * 86  # 87 chars b64url
AUTH = "a" * 22


def _sub(endpoint=APPLE, p256dh=P256DH, auth=AUTH):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


@pytest.fixture
def wp_on(monkeypatch, tmp_path, tmp_db):
    monkeypatch.setattr(settings, "webpush_enabled", True, raising=False)
    monkeypatch.setattr(settings, "webpush_subject", "mailto:test@example.com", raising=False)
    monkeypatch.setattr(settings, "webpush_key_path", str(tmp_path / "keys" / "vapid.pem"), raising=False)
    monkeypatch.setattr(settings, "webpush_show_text", True, raising=False)
    monkeypatch.setattr(settings, "webpush_ttl_seconds", 3600, raising=False)
    monkeypatch.setattr(webpush_manager, "_vapid", None)
    monkeypatch.setattr(webpush_manager, "_send_sem", None)
    webpush_manager._refresh_count()
    yield settings
    webpush_manager._vapid = None


@pytest.fixture
def wp_off(monkeypatch, tmp_db):
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    monkeypatch.setattr(webpush_manager, "_send_sem", None)
    webpush_manager._refresh_count()
    yield settings


def _rows():
    with state_store.get_db() as conn:
        return {r["endpoint"]: dict(r) for r in conn.execute("SELECT * FROM webpush_subscriptions").fetchall()}


def _insert(endpoint, created_at=None, last_ok_at=None):
    with state_store.get_db() as conn:
        conn.execute(
            "INSERT INTO webpush_subscriptions (endpoint, p256dh, auth, created_at, last_ok_at) VALUES (?, ?, ?, ?, ?)",
            (endpoint, P256DH, AUTH, created_at or datetime.now().isoformat(), last_ok_at),
        )
    webpush_manager._refresh_count()


def _send_total(result):
    from orchestrator.metrics import WEBPUSH_SEND_TOTAL

    return WEBPUSH_SEND_TOTAL.labels(result=result)._value.get()


# ===========================================================================
# endpoint_error — SSRF allow-list
# ===========================================================================


@pytest.mark.parametrize(
    "endpoint",
    [
        APPLE,
        FCM,
        MOZ,
        "https://web.push.apple.com:443/abc",
        "https://WEB.PUSH.APPLE.COM/abc",
        "https://android.googleapis.com/gcm/send/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
    ],
)
def test_endpoint_allowed(endpoint):
    assert webpush_manager.endpoint_error(endpoint) is None


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://web.push.apple.com/abc", "bad_endpoint"),
        ("https://web.push.apple.com:8443/abc", "bad_endpoint"),
        ("https://x@web.push.apple.com/abc", "bad_endpoint"),
        ("https://user:pw@fcm.googleapis.com/x", "bad_endpoint"),
        ("https://10.0.0.248\\.push.apple.com/", "bad_endpoint"),
        ("https://evil.example\\.push.apple.com/x", "bad_endpoint"),
        ("https://web.push.apple.com/a b", "bad_endpoint"),
        (" https://web.push.apple.com/abc", "bad_endpoint"),
        ("https://web.push.apple.com/abc\n", "bad_endpoint"),
        ("https://web.push.apple.com/\tabc", "bad_endpoint"),
        ("https://web.push.apple.com/\x00", "bad_endpoint"),
        ("https://evil-fcm.googleapis.com/fcm/send/x", "push_service_not_allowed"),
        ("https://fcm.googleapis.com.evil.com/x", "push_service_not_allowed"),
        ("https://push.apple.com.evil.com/x", "push_service_not_allowed"),
        ("https://push.apple.com/x", "push_service_not_allowed"),  # bare apex, not a subdomain
        ("https://evilpush.apple.com/x", "push_service_not_allowed"),
        ("https://evil.com#.push.apple.com", "push_service_not_allowed"),
        ("https://evil.com?.push.apple.com", "push_service_not_allowed"),
        ("https://10.0.0.248/x", "push_service_not_allowed"),
        ("https://[::1]/x", "bad_endpoint"),  # urlsplit/urllib3 disagree on the bracketed host
        ("https://web.push.apple.com/" + "a" * 1100, "bad_endpoint"),
        ("https://web.push.apple.com:99999/x", "bad_endpoint"),
        ("https:///x", "bad_endpoint"),
        ("ftp://web.push.apple.com/x", "bad_endpoint"),
        ("", "bad_endpoint"),
        (None, "bad_endpoint"),
        (123, "bad_endpoint"),
        (["https://web.push.apple.com/abc"], "bad_endpoint"),
    ],
)
def test_endpoint_denied(endpoint, expected):
    assert webpush_manager.endpoint_error(endpoint) == expected


# ===========================================================================
# parse_subscription
# ===========================================================================


def test_parse_subscription_ok():
    sub, err = webpush_manager.parse_subscription(_sub())
    assert err is None
    assert sub == {"endpoint": APPLE, "p256dh": P256DH, "auth": AUTH}


def test_parse_subscription_accepts_padding():
    sub, err = webpush_manager.parse_subscription(_sub(auth="a" * 22 + "=="))
    assert err is None


@pytest.mark.parametrize(
    "body,expected",
    [
        (None, "bad_body"),
        ([], "bad_body"),
        ("x", "bad_body"),
        ({}, "bad_endpoint"),
        ({"endpoint": "http://evil/"}, "bad_endpoint"),
        ({"endpoint": "https://evil.example/x", "keys": {}}, "push_service_not_allowed"),
        ({"endpoint": APPLE}, "bad_keys"),
        ({"endpoint": APPLE, "keys": "x"}, "bad_keys"),
        ({"endpoint": APPLE, "keys": {"auth": AUTH}}, "bad_keys"),
        ({"endpoint": APPLE, "keys": {"p256dh": P256DH}}, "bad_keys"),
        (_sub(p256dh="A" * 10), "bad_keys"),
        (_sub(p256dh="A" * 200), "bad_keys"),
        (_sub(p256dh="A" * 86 + "+"), "bad_keys"),  # std b64, not url-safe
        (_sub(p256dh="A" * 86 + "/"), "bad_keys"),
        (_sub(p256dh=12345), "bad_keys"),
        (_sub(auth="a" * 8), "bad_keys"),
        (_sub(auth="a" * 40), "bad_keys"),
        (_sub(auth="a" * 21 + "!"), "bad_keys"),
        (_sub(auth="a" * 20 + "\n"), "bad_keys"),
    ],
)
def test_parse_subscription_rejects(body, expected):
    sub, err = webpush_manager.parse_subscription(body)
    assert sub is None
    assert err == expected


# ===========================================================================
# save / delete / prune / has_subscribers
# ===========================================================================


def test_save_subscription_upsert_and_count(wp_on):
    s, _ = webpush_manager.parse_subscription(_sub())
    assert webpush_manager.save_subscription(s) is None
    assert webpush_manager.subscriber_count() == 1
    # re-subscribe same endpoint with new keys → update, not a new row
    s2, _ = webpush_manager.parse_subscription(_sub(auth="b" * 22))
    assert webpush_manager.save_subscription(s2) is None
    rows = _rows()
    assert len(rows) == 1
    assert rows[APPLE]["auth"] == "b" * 22


def test_save_subscription_cap(wp_on):
    for i in range(webpush_manager.MAX_SUBSCRIPTIONS):
        s, _ = webpush_manager.parse_subscription(_sub(endpoint=f"{APPLE}{i}"))
        assert webpush_manager.save_subscription(s) is None
    extra, _ = webpush_manager.parse_subscription(_sub(endpoint=f"{APPLE}-extra"))
    assert webpush_manager.save_subscription(extra) == "too_many_devices"
    assert len(_rows()) == webpush_manager.MAX_SUBSCRIPTIONS
    # an existing device can still refresh its keys at the cap
    existing, _ = webpush_manager.parse_subscription(_sub(endpoint=f"{APPLE}0", auth="c" * 22))
    assert webpush_manager.save_subscription(existing) is None


def test_delete_subscription(wp_on):
    _insert(APPLE)
    assert webpush_manager.delete_subscription(APPLE) is True
    assert webpush_manager.delete_subscription(APPLE) is False
    assert webpush_manager.subscriber_count() == 0


def test_prune_stale_subscriptions(wp_on):
    old = (datetime.now() - timedelta(days=40)).isoformat()
    _insert(f"{APPLE}-dead", created_at=old)
    _insert(f"{APPLE}-old-but-ok", created_at=old, last_ok_at=datetime.now().isoformat())
    _insert(f"{APPLE}-stale-ok", created_at=old, last_ok_at=old)
    _insert(f"{APPLE}-new")
    assert webpush_manager.prune_stale_subscriptions() == 2
    assert set(_rows()) == {f"{APPLE}-old-but-ok", f"{APPLE}-new"}
    assert webpush_manager.subscriber_count() == 2


def test_has_subscribers_none(wp_on):
    assert webpush_manager.has_subscribers() is False


def test_has_subscribers_recently_created(wp_on):
    _insert(APPLE)
    assert webpush_manager.has_subscribers() is True


def test_has_subscribers_recently_ok(wp_on):
    _insert(APPLE, created_at=(datetime.now() - timedelta(days=60)).isoformat(), last_ok_at=datetime.now().isoformat())
    assert webpush_manager.has_subscribers() is True


def test_has_subscribers_long_dead_row(wp_on):
    old = (datetime.now() - timedelta(days=20)).isoformat()
    _insert(APPLE, created_at=old)
    _insert(FCM, created_at=old, last_ok_at=old)
    assert webpush_manager.subscriber_count() == 2
    assert webpush_manager.has_subscribers() is False


def test_has_subscribers_false_when_disabled(wp_off):
    _insert(APPLE)
    assert webpush_manager.has_subscribers() is False


# ===========================================================================
# send_to_all (with _send_one faked)
# ===========================================================================


async def test_send_to_all_bookkeeping(wp_on, monkeypatch):
    gone, ok, auth, err = f"{APPLE}-gone", f"{APPLE}-ok", f"{APPLE}-auth", f"{APPLE}-5xx"
    for ep in (gone, ok, auth, err):
        _insert(ep)
    outcomes = {gone: "gone", ok: "ok", auth: "auth", err: "http_5xx"}
    payloads = []

    def _fake(sub, data):
        payloads.append(data)
        return outcomes[sub["endpoint"]]

    monkeypatch.setattr(webpush_manager, "_send_one", _fake)
    before_ok = _send_total("ok")

    counts = await webpush_manager.send_to_all("Jess", "hello", url="/chat", tag="t1")

    assert counts == {"sent": 1, "failed": 3}
    rows = _rows()
    assert gone not in rows  # 404/410 → deleted
    assert auth in rows and rows[auth]["last_ok_at"] is None  # kept, must re-subscribe
    assert err in rows and rows[err]["last_ok_at"] is None
    assert rows[ok]["last_ok_at"] is not None
    assert webpush_manager.subscriber_count() == 3
    assert _send_total("ok") == before_ok + 1
    import json

    assert json.loads(payloads[0]) == {"title": "Jess", "body": "hello", "url": "/chat", "tag": "t1"}


async def test_send_to_all_exception_in_send_counts_failed(wp_on, monkeypatch):
    _insert(APPLE)

    def _boom(sub, data):
        raise RuntimeError("x")

    monkeypatch.setattr(webpush_manager, "_send_one", _boom)
    assert await webpush_manager.send_to_all("Jess", "x") == {"sent": 0, "failed": 1}
    assert APPLE in _rows()


async def test_send_to_all_disabled_sends_nothing(wp_off, monkeypatch):
    _insert(APPLE)
    calls = []
    monkeypatch.setattr(webpush_manager, "_send_one", lambda s, d: calls.append(s) or "ok")
    assert await webpush_manager.send_to_all("Jess", "x") == {"sent": 0, "failed": 0}
    assert calls == []


async def test_send_to_all_no_subscribers(wp_on, monkeypatch):
    calls = []
    monkeypatch.setattr(webpush_manager, "_send_one", lambda s, d: calls.append(s) or "ok")
    before = _send_total("no_subscribers")
    assert await webpush_manager.send_to_all("Jess", "x") == {"sent": 0, "failed": 0}
    assert calls == []
    assert _send_total("no_subscribers") == before + 1


# ===========================================================================
# fire_inbox_push
# ===========================================================================


@pytest.fixture
def capture_send(monkeypatch):
    calls = []

    async def _fake(title, body, url="/chat", tag=None):
        calls.append({"title": title, "body": body, "url": url, "tag": tag})
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(webpush_manager, "send_to_all", _fake)
    return calls


async def _drain():
    pending = list(webpush_manager._bg_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_fire_inbox_push_reminder_shows_text(wp_on, capture_send):
    _insert(APPLE)
    webpush_manager.fire_inbox_push({"id": "i1", "kind": "reminder", "ref": "r1", "text": "take bins out"})
    await _drain()
    assert capture_send == [{"title": "Jess", "body": "take bins out", "url": "/chat", "tag": "i1"}]


async def test_fire_inbox_push_medication_is_generic_even_with_show_text(wp_on, capture_send):
    _insert(APPLE)
    assert settings.webpush_show_text is True
    webpush_manager.fire_inbox_push({"id": "i2", "kind": "selfcare", "ref": "medication", "text": "Take DrugName 10mg"})
    await _drain()
    assert len(capture_send) == 1
    assert capture_send[0]["body"] == "Check-in waiting"
    assert "DrugName" not in capture_send[0]["body"]


async def test_fire_inbox_push_other_selfcare_shows_text(wp_on, capture_send):
    _insert(APPLE)
    webpush_manager.fire_inbox_push({"id": "i3", "kind": "selfcare", "ref": "water", "text": "drink water"})
    await _drain()
    assert capture_send[0]["body"] == "drink water"


async def test_fire_inbox_push_show_text_off(wp_on, capture_send, monkeypatch):
    monkeypatch.setattr(settings, "webpush_show_text", False, raising=False)
    _insert(APPLE)
    webpush_manager.fire_inbox_push({"id": "i4", "kind": "reminder", "ref": "r", "text": "secret"})
    await _drain()
    assert capture_send[0]["body"] == "Reminder waiting"


async def test_fire_inbox_push_truncates_and_handles_empty(wp_on, capture_send):
    _insert(APPLE)
    webpush_manager.fire_inbox_push({"id": "i5", "kind": "reminder", "ref": "r", "text": "x" * 500})
    webpush_manager.fire_inbox_push({"id": "i6", "kind": "reminder", "ref": "r", "text": ""})
    await _drain()
    bodies = sorted((c["body"] for c in capture_send), key=len)
    assert bodies[0] == "Jess"
    assert len(bodies[1]) == 240


async def test_fire_inbox_push_no_subscribers_or_disabled(wp_on, capture_send, monkeypatch):
    webpush_manager.fire_inbox_push({"id": "i7", "kind": "reminder", "ref": "r", "text": "x"})
    await _drain()
    assert capture_send == []
    monkeypatch.setattr(settings, "webpush_enabled", False, raising=False)
    _insert(APPLE)
    webpush_manager.fire_inbox_push({"id": "i8", "kind": "reminder", "ref": "r", "text": "x"})
    await _drain()
    assert capture_send == []


# ===========================================================================
# _send_one with pywebpush faked
# ===========================================================================


@pytest.mark.parametrize(
    "status,expected",
    [(410, "gone"), (404, "gone"), (403, "auth"), (401, "auth"), (413, "http_4xx"), (500, "http_5xx"), (None, "error")],
)
def test_send_one_maps_push_service_status(wp_on, monkeypatch, status, expected):
    import pywebpush

    seen = {}

    def _raise(**kw):
        seen.update(kw)
        resp = SimpleNamespace(status_code=status, text="") if status is not None else None
        raise pywebpush.WebPushException("push failed", response=resp)

    monkeypatch.setattr(pywebpush, "webpush", _raise)
    sub = {"endpoint": APPLE, "p256dh": P256DH, "auth": AUTH}
    assert webpush_manager._send_one(sub, "{}") == expected
    assert seen["requests_session"].max_redirects == 0
    assert seen["vapid_claims"] == {"sub": "mailto:test@example.com"}
    assert seen["ttl"] == 3600


def test_send_one_ok(wp_on, monkeypatch):
    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: SimpleNamespace(status_code=201))
    assert webpush_manager._send_one({"endpoint": APPLE, "p256dh": P256DH, "auth": AUTH}, "{}") == "ok"


def test_send_one_timeout_and_redirect(wp_on, monkeypatch):
    import pywebpush
    import requests

    def _timeout(**kw):
        raise requests.Timeout()

    def _redir(**kw):
        raise requests.TooManyRedirects()

    sub = {"endpoint": APPLE, "p256dh": P256DH, "auth": AUTH}
    monkeypatch.setattr(pywebpush, "webpush", _timeout)
    assert webpush_manager._send_one(sub, "{}") == "timeout"
    monkeypatch.setattr(pywebpush, "webpush", _redir)
    assert webpush_manager._send_one(sub, "{}") == "redirect"


def test_send_one_rechecks_endpoint(wp_on, monkeypatch):
    import pywebpush

    called = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: called.append(kw))
    assert webpush_manager._send_one({"endpoint": "https://10.0.0.248/x", "p256dh": P256DH, "auth": AUTH}, "{}") == (
        "error"
    )
    assert called == []


def test_send_one_real_encryption_smoke(wp_on, monkeypatch):
    """Full pywebpush path (real ECDH + aes128gcm + VAPID JWT) with only the
    HTTP POST faked: proves our arguments are accepted by pywebpush 2.0.3."""
    import requests
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    client_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = (
        base64.urlsafe_b64encode(client_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))
        .rstrip(b"=")
        .decode()
    )
    auth = base64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode()
    sub, err = webpush_manager.parse_subscription(_sub(p256dh=p256dh, auth=auth))
    assert err is None

    posted = {}

    def _post(self, url, data=None, headers=None, timeout=None, **kw):
        posted.update(url=url, data=data, headers=headers, timeout=timeout, kw=kw)
        return SimpleNamespace(status_code=posted.get("status", 201), text="", headers={})

    monkeypatch.setattr(requests.Session, "post", _post)
    assert webpush_manager._send_one(sub, '{"title":"Jess"}') == "ok"
    assert posted["url"] == APPLE
    assert posted["headers"]["Urgency"] == "high"
    assert posted["headers"]["content-encoding"] == "aes128gcm"
    assert posted["headers"]["authorization"].startswith("vapid t=")
    assert b"Jess" not in posted["data"]  # encrypted
    assert posted["timeout"] == 10

    def _post410(self, url, data=None, headers=None, timeout=None, **kw):
        return SimpleNamespace(status_code=410, reason="Gone", text="gone", headers={})

    monkeypatch.setattr(requests.Session, "post", _post410)
    assert webpush_manager._send_one(sub, "{}") == "gone"


# ===========================================================================
# Key handling
# ===========================================================================


def test_public_key_is_87_char_b64url_and_key_file_0600(wp_on):
    import os
    import stat

    key = webpush_manager.public_key()
    assert len(key) == 87
    assert all(c.isalnum() or c in "-_" for c in key)
    raw = base64.urlsafe_b64decode(key + "=")
    assert len(raw) == 65 and raw[0] == 4
    mode = stat.S_IMODE(os.stat(settings.webpush_key_path).st_mode)
    assert mode == 0o600


def test_public_key_stable_across_reload(wp_on):
    k1 = webpush_manager.public_key()
    webpush_manager._vapid = None
    assert webpush_manager.public_key() == k1


def test_public_key_none_when_disabled(wp_off):
    assert webpush_manager.public_key() is None


# ===========================================================================
# routes_webpush
# ===========================================================================


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_route_public_key_disabled(client, wp_off):
    r = client.get("/api/webpush/public-key")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": False, "public_key": None, "devices": 0}


def test_route_public_key_enabled(client, wp_on):
    r = client.get("/api/webpush/public-key")
    body = r.json()
    assert body["ok"] is True and body["enabled"] is True
    assert len(body["public_key"]) == 87
    assert body["devices"] == 0


def test_route_subscribe_disabled_409(client, wp_off):
    r = client.post("/api/webpush/subscribe", json=_sub())
    assert r.status_code == 409
    assert r.json()["error"] == "disabled"
    assert _rows() == {}


@pytest.mark.parametrize(
    "body,err",
    [
        (_sub(endpoint="https://10.0.0.248\\.push.apple.com/"), "bad_endpoint"),
        (_sub(endpoint="https://evil.example/x"), "push_service_not_allowed"),
        (_sub(auth="!"), "bad_keys"),
        ([1, 2], "bad_body"),
    ],
)
def test_route_subscribe_bad_body_400(client, wp_on, body, err):
    r = client.post("/api/webpush/subscribe", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == err
    assert _rows() == {}


def test_route_subscribe_non_json_400(client, wp_on):
    r = client.post("/api/webpush/subscribe", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_route_subscribe_ok_then_unsubscribe(client, wp_on):
    r = client.post("/api/webpush/subscribe", json=_sub())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "devices": 1}
    r = client.post("/api/webpush/unsubscribe", json={"endpoint": APPLE})
    assert r.json() == {"ok": True, "removed": True, "devices": 0}


def test_route_subscribe_cap_409(client, wp_on):
    for i in range(webpush_manager.MAX_SUBSCRIPTIONS):
        _insert(f"{APPLE}{i}")
    r = client.post("/api/webpush/subscribe", json=_sub(endpoint=f"{APPLE}-new"))
    assert r.status_code == 409
    assert r.json()["error"] == "too_many_devices"


def test_route_unsubscribe_bad_endpoint(client, wp_on):
    r = client.post("/api/webpush/unsubscribe", json={"endpoint": "http://x"})
    assert r.status_code == 400
    r = client.post("/api/webpush/unsubscribe", json=[1])
    assert r.status_code == 400


def test_route_test_disabled_409(client, wp_off):
    assert client.post("/api/webpush/test").status_code == 409


def test_route_test_counts(client, wp_on, monkeypatch):
    _insert(APPLE)
    monkeypatch.setattr(webpush_manager, "_send_one", lambda s, d: "ok")
    r = client.post("/api/webpush/test")
    assert r.json() == {"ok": True, "sent": 1, "failed": 0}


# ===========================================================================
# config validator
# ===========================================================================


@pytest.mark.parametrize(
    "subject,enabled",
    [
        ("mailto:me@example.com", True),
        ("https://dash.example", True),
        ("", False),
        ("mailto:nobody", False),
        ("http://dash.example", False),
        ("me@example.com", False),
    ],
)
def test_config_validator_subject(subject, enabled):
    from orchestrator.config import Settings

    s = Settings(webpush_enabled=True, webpush_subject=subject, webpush_ttl_seconds=5)
    assert s.webpush_enabled is enabled
    assert s.webpush_ttl_seconds == 60
