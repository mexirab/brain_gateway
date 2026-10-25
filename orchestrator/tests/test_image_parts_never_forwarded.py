"""
No image part ever reaches a model loop (security review of PR #148, 2026-10-25).

Before this, ``CloudBrain._process_vision`` returned early when vision was off
or extraction found nothing, which skipped the pass that turns image parts
into text. So these went to the multimodal brain unchanged, without the
untrusted PHOTO CONTENT framing:

- an http(s) ``image_url`` in a user turn (extraction rejects it as SSRF
  prevention, then it was forwarded anyway, and llama-server downloads http(s)
  image URLs itself: blind GET SSRF from Helios);
- image parts in assistant turns (extraction only reads user turns);
- every image while ``VISION_ENABLED=false`` (the default).

Now every content list becomes a string: text kept, each user image a framed
description or a NOT ANALYZED note appended after the text, other roles text
only, other part types dropped. Dashboard photos (data URIs) and Telegram
photo turns (already a string) behave as before.
"""

import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import shared
from orchestrator.cloud_brain import CloudBrain, _flatten_content, _sanitize_client_history, _strip_photo_content
from orchestrator.metrics import VISION_REQUEST_COUNT
from orchestrator.prompt_builder import last_user_text
from orchestrator.unified_loop import _last_user_text
from orchestrator.vision_handler import (
    NOT_ANALYZED_DISABLED,
    NOT_ANALYZED_NOT_INLINE,
    NOT_ANALYZED_OVER_CAP,
    PHOTO_BLOCK_RE,
    _is_safe_image_url,
    extract_images_from_messages,
    frame_photo_description,
)

DATA_URI = "data:image/jpeg;base64," + "A" * 400
LINK = "http://127.0.0.1:9/ssrf-probe.png"
LINKS = [
    LINK,
    "https://example.invalid/cat.png",
    "HTTP://127.0.0.1/upper.png",
    "//127.0.0.1/protocol-relative.png",
    "gopher://127.0.0.1:70/x",
    "file:///etc/passwd",
    "ftp://127.0.0.1/x.png",
    "data-but-not-really:http://x",
]


def _img(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _status(status: str) -> float:
    return VISION_REQUEST_COUNT.labels(status=status)._value.get()


def _assert_text_only(messages):
    """What a loop may be given: every content a string, no URL, no base64."""
    for m in messages:
        assert isinstance(m["content"], str), m
    dumped = json.dumps(messages)
    assert "image_url" not in dumped
    assert "127.0.0.1" not in dumped
    assert "A" * 100 not in dumped  # no base64 payload


@pytest.fixture
def vision_on(monkeypatch):
    monkeypatch.setattr(shared, "VISION_ENABLED", True)
    monkeypatch.setattr(shared, "_vision_image_cache", {})


@pytest.fixture
def vision_off(monkeypatch):
    monkeypatch.setattr(shared, "VISION_ENABLED", False)
    monkeypatch.setattr(shared, "_vision_image_cache", {})


def _brain():
    brain = CloudBrain.__new__(CloudBrain)
    brain._model_name = "fake"
    return brain


@contextlib.contextmanager
def _vision(analyze, healthy=True):
    with (
        patch("orchestrator.vision_handler.check_vision_health", AsyncMock(return_value=healthy)),
        patch("orchestrator.vision_handler.analyze_image", analyze),
    ):
        yield


# ---------------------------------------------------------------------------
# What counts as inline image data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [DATA_URI, "data:image/png;base64,iVBORw0KGgo=", "/9j/4AAQSkZJRg=="])
def test_inline_image_data_is_safe(url):
    assert _is_safe_image_url(url)


@pytest.mark.parametrize("url", [*LINKS, "", "not base64!", None, 42, ["x"], {"url": "x"}])
def test_links_and_junk_are_not_inline(url):
    assert not _is_safe_image_url(url)


def test_extraction_skips_links_and_malformed_parts():
    messages = [
        {
            "role": "user",
            "content": [
                _img(LINK),
                {"type": "image_url", "image_url": {"url": ["not", "a", "string"]}},
                {"type": "image_url", "image_url": 7},
                _img(DATA_URI),
            ],
        }
    ]
    images = extract_images_from_messages(messages)
    assert [i["image_url"] for i in images] == [DATA_URI]


# ---------------------------------------------------------------------------
# _process_vision: the flatten pass always runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", LINKS)
async def test_link_in_user_turn_is_not_forwarded_vision_on(vision_on, url):
    analyze = AsyncMock(return_value="unused")
    before = _status("rejected_url")
    routing: dict = {}
    messages = [{"role": "user", "content": [_text("what is this?"), _img(url)]}]
    with _vision(analyze):
        out = await _brain()._process_vision(messages, routing)

    analyze.assert_not_awaited()
    assert out[0]["content"] == "what is this?\n\n[PHOTO 1 NOT ANALYZED] " + NOT_ANALYZED_NOT_INLINE
    assert url not in out[0]["content"]
    assert _status("rejected_url") == before + 1
    assert routing["images_not_analyzed"] == 1
    assert "vision_images" not in routing
    assert shared._vision_image_cache == {}


async def test_link_in_user_turn_is_not_forwarded_vision_off(vision_off):
    out = await _brain()._process_vision([{"role": "user", "content": [_img(LINK)]}], {})
    assert out[0]["content"] == "[PHOTO 1 NOT ANALYZED] " + NOT_ANALYZED_NOT_INLINE
    _assert_text_only(out)


async def test_vision_off_flattens_photos_to_not_analyzed(vision_off, caplog):
    before = _status("disabled")
    routing: dict = {}
    analyze = AsyncMock(return_value="unused")
    messages = [{"role": "user", "content": [_text("read this"), _img(DATA_URI), _img(DATA_URI + "B")]}]
    with _vision(analyze), caplog.at_level(logging.INFO):
        out = await _brain()._process_vision(messages, routing)

    analyze.assert_not_awaited()
    assert out[0]["content"] == (
        f"read this\n\n[PHOTO 1 NOT ANALYZED] {NOT_ANALYZED_DISABLED}\n\n[PHOTO 2 NOT ANALYZED] {NOT_ANALYZED_DISABLED}"
    )
    _assert_text_only(out)
    assert _status("disabled") == before + 2
    assert routing["images_not_analyzed"] == 2
    assert shared._vision_image_cache == {}  # nothing cached for a tool that can't run
    assert "Vision disabled — 2 image(s) not analyzed" in caplog.text


async def test_assistant_image_parts_never_forwarded(vision_on):
    analyze = AsyncMock(return_value="unused")
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [_text("here you go"), _img(LINK), _img(DATA_URI)]},
        {"role": "user", "content": "thanks"},
    ]
    with _vision(analyze):
        out = await _brain()._process_vision(messages, {})

    analyze.assert_not_awaited()
    assert out[1] == {"role": "assistant", "content": "here you go"}
    _assert_text_only(out)


async def test_mixed_photo_and_link_keep_part_order(vision_on):
    analyze = AsyncMock(return_value="A grocery receipt")
    messages = [{"role": "user", "content": [_text("compare"), _img(LINK), _img(DATA_URI)]}]
    with _vision(analyze):
        out = await _brain()._process_vision(messages, {})

    analyze.assert_awaited_once()
    assert analyze.await_args.args[0] == DATA_URI
    assert out[0]["content"] == (
        "compare\n\n"
        f"[PHOTO 1 NOT ANALYZED] {NOT_ANALYZED_NOT_INLINE}\n\n" + frame_photo_description(2, "A grocery receipt")
    )


async def test_photos_past_the_cap_are_noted_not_forwarded(vision_on):
    before = _status("skipped_cap")
    analyze = AsyncMock(side_effect=["one", "two", "three"])
    urls = [DATA_URI + c for c in "BCDE"]
    with _vision(analyze):
        out = await _brain()._process_vision([{"role": "user", "content": [_img(u) for u in urls]}], {})

    assert analyze.await_count == 3
    content = out[0]["content"]
    assert content.endswith(f"[PHOTO 4 NOT ANALYZED] {NOT_ANALYZED_OVER_CAP}")
    assert "[PHOTO 3 CONTENT" in content
    _assert_text_only(out)
    assert _status("skipped_cap") == before + 1


async def test_other_part_types_are_dropped(vision_off, caplog):
    messages = [
        {
            "role": "user",
            "content": [
                _text("listen"),
                {"type": "input_audio", "input_audio": {"url": LINK, "format": "wav"}},
                {"type": "video_url", "video_url": {"url": LINK}},
                {"type": "input_video", "input_video": {"data": "AAAA"}},
                {"type": "text", "text": {"nested": "not a string"}},
                12345,
                _img(DATA_URI),
            ],
        }
    ]
    with caplog.at_level(logging.INFO):
        out = await _brain()._process_vision(messages, {})
    assert out[0]["content"] == f"listen\n\n[PHOTO 1 NOT ANALYZED] {NOT_ANALYZED_DISABLED}"
    _assert_text_only(out)
    assert "Dropped 5 non-text, non-image content part(s)" in caplog.text


async def test_string_turns_pass_through_unchanged(vision_off):
    # Telegram photo turns arrive as a string with the framed description.
    tg = {"role": "user", "content": "[I sent you a photo.]\n\n" + frame_photo_description(1, "a cat")}
    plain = {"role": "assistant", "content": "ok"}
    out = await _brain()._process_vision([tg, plain], {})
    assert out[0] is tg and out[1] is plain


async def test_dashboard_photo_still_framed_and_cached(vision_on):
    # The dashboard sends one data-URI photo with the turn it belongs to.
    analyze = AsyncMock(return_value="A handwritten shopping list")
    routing: dict = {}
    messages = [
        {"role": "user", "content": "[Shared a photo] earlier one"},
        {"role": "assistant", "content": "Nice."},
        {"role": "user", "content": [_text("what does it say?"), _img(DATA_URI)]},
    ]
    with _vision(analyze):
        out = await _brain()._process_vision(messages, routing)

    assert out[2]["content"] == "what does it say?\n\n" + frame_photo_description(1, "A handwritten shopping list")
    assert out[0] is messages[0] and out[1] is messages[1]
    assert routing == {"vision_images": 1}
    assert list(shared._vision_image_cache.values()) == [DATA_URI]


async def test_not_analyzed_notes_stay_out_of_routing_and_memory(vision_off):
    out = await _brain()._process_vision(
        [{"role": "user", "content": [_text("turn off the lights"), _img(LINK), _img(DATA_URI)]}], {}
    )
    turn = out[0]["content"]
    assert PHOTO_BLOCK_RE.sub("", turn).strip() == "turn off the lights"
    assert _last_user_text(out) == "turn off the lights"
    assert last_user_text(out) == "turn off the lights"
    assert _strip_photo_content(out[0])["content"] == "turn off the lights\n\n[photo]"


def test_flatten_never_mutates_the_input():
    msg = {"role": "user", "content": [_text("x"), _img(LINK)]}
    snapshot = json.dumps(msg)
    _flatten_content([msg], {}, vision_on=True)
    assert json.dumps(msg) == snapshot


# ---------------------------------------------------------------------------
# _sanitize_client_history: assistant turns are text only
# ---------------------------------------------------------------------------


def test_sanitize_flattens_assistant_turns_and_drops_image_only_ones(caplog):
    client = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [_img(LINK)]},
        {"role": "assistant", "content": [_text("Sure."), _img(DATA_URI)]},
        {"role": "user", "content": [_text("look"), _img(DATA_URI)]},
    ]
    snapshot = json.dumps(client)
    with caplog.at_level(logging.INFO, logger="orchestrator.cloud_brain"):
        out = _sanitize_client_history(client)

    assert out[0] is client[0]
    assert out[1] == {"role": "assistant", "content": "Sure."}
    assert out[2] is client[3]  # user photos are vision's job, untouched here
    assert len(out) == 3
    assert json.dumps(client) == snapshot
    assert "Dropped 2 non-text part(s) from client assistant turns" in caplog.text
    assert "Dropped 1 empty assistant message(s) from history" in caplog.text
    assert "127.0.0.1" not in caplog.text


# ---------------------------------------------------------------------------
# End to end: what each loop path is handed
# ---------------------------------------------------------------------------

PRIMARY_URL = "http://primary.invalid/v1"
FALLBACK_URL = "http://fallback.invalid/v1"


def _client_messages():
    return [
        {"role": "user", "content": [_text("what's in this?"), _img(LINK)]},
        {"role": "assistant", "content": [_text("Let me see."), _img("https://example.invalid/x.png")]},
        {"role": "user", "content": [_text("and this one?"), _img(DATA_URI)]},
    ]


def _make_brain(monkeypatch, loop_mock, *, healthy=True):
    from orchestrator import cloud_brain

    brain = CloudBrain.__new__(CloudBrain)
    brain._mode_router = SimpleNamespace(route=lambda text: SimpleNamespace(mode="baseline", intensity="low", tags=[]))
    brain._is_greeting = lambda text: True
    brain._last_user_text = last_user_text
    brain._get_unified_system_prompt = lambda *a, **k: "sys"
    brain._check_model_health = AsyncMock(return_value=healthy)
    brain._start_model_server = AsyncMock(return_value=False)
    brain._model_url = PRIMARY_URL
    brain._model_name = "primary"
    brain._fallback_model_url = FALLBACK_URL
    brain._fallback_model_name = "fallback"
    brain._get_all_tools = lambda: []
    brain._run_unified_loop = loop_mock
    brain._schedule_auto_learn = lambda messages: None
    brain._try_fast_path = AsyncMock()  # must not run on a photo turn
    monkeypatch.setattr(cloud_brain, "record_reply_style", lambda mode: None)
    monkeypatch.setattr(cloud_brain, "apply_tone_request", lambda text: "")
    monkeypatch.setattr(cloud_brain, "is_first_chat", lambda: False)
    monkeypatch.setattr(shared, "REAL_STREAMING_ENABLED", True)
    return brain


async def _drain(response):
    return "".join([c if isinstance(c, str) else c.decode() async for c in response.body_iterator])


@pytest.mark.parametrize("vision", [True, False])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("healthy", [True, False])  # False = health-check fallback path
async def test_no_loop_path_is_handed_an_image_part(monkeypatch, vision, stream, healthy):
    monkeypatch.setattr(shared, "VISION_ENABLED", vision)
    monkeypatch.setattr(shared, "_vision_image_cache", {})
    loop_mock = AsyncMock(return_value="fine")
    brain = _make_brain(monkeypatch, loop_mock, healthy=healthy)
    analyze = AsyncMock(return_value="A cat on a sofa")
    client = _client_messages()
    snapshot = json.dumps(client)

    with _vision(analyze):
        resp = await brain.chat(client, stream=stream, ha_client=object())
        body = await _drain(resp) if stream else resp.body.decode()

    assert "fine" in body
    assert json.dumps(client) == snapshot
    assert loop_mock.await_count == 1
    kwargs = loop_mock.call_args.kwargs
    assert kwargs["model_url"] == (PRIMARY_URL if healthy else FALLBACK_URL)
    seen = kwargs["messages"]
    _assert_text_only(seen)
    assert seen[0]["content"] == "what's in this?\n\n[PHOTO 1 NOT ANALYZED] " + NOT_ANALYZED_NOT_INLINE
    assert seen[1] == {"role": "assistant", "content": "Let me see."}
    expected_photo = (
        frame_photo_description(1, "A cat on a sofa") if vision else f"[PHOTO 1 NOT ANALYZED] {NOT_ANALYZED_DISABLED}"
    )
    assert seen[2]["content"] == "and this one?\n\n" + expected_photo
    assert analyze.await_count == (1 if vision else 0)
    brain._try_fast_path.assert_not_awaited()
