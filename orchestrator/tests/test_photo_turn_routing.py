"""Photo turns from the dashboard chat (multi-part content).

Regression: `_last_user_text` used to skip list content and return the
PREVIOUS user turn, so sending a photo after "turn off the kitchen lights"
re-ran that Home Assistant command via the fast-path and never answered the
photo. Also: vision text appended to a photo turn is not the user's words, so
it must not drive the selfcare auto-log.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestrator.unified_loop import _detect_selfcare_action, _last_user_text

_IMG = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}


def test_photo_only_turn_does_not_fall_back_to_previous_text():
    messages = [
        {"role": "user", "content": "turn off the kitchen lights"},
        {"role": "assistant", "content": "Done!"},
        {"role": "user", "content": [_IMG]},
    ]
    assert _last_user_text(messages) == ""


def test_photo_turn_returns_its_own_text_parts():
    messages = [
        {"role": "user", "content": "turn off the kitchen lights"},
        {"role": "user", "content": [{"type": "text", "text": "what is this?"}, _IMG]},
    ]
    assert _last_user_text(messages) == "what is this?"


def test_plain_text_and_owui_task_skip_unchanged():
    messages = [
        {"role": "user", "content": "hello there"},
        {"role": "user", "content": "### Task: generate a title"},
    ]
    assert _last_user_text(messages) == "hello there"


def test_vision_analysis_text_is_stripped():
    # After cloud_brain._process_vision the photo turn is a string with the
    # analysis appended; a photographed note must not auto-log a meal.
    processed = [
        {"role": "user", "content": "what does this say?\n\n[Image 1 Analysis]\nA note reading: I ate lunch already"}
    ]
    text = _last_user_text(processed)
    assert text == "what does this say?"
    assert _detect_selfcare_action(text) is None


def _make_brain(loop_mock, fast_path_mock, vision_images=1):
    from orchestrator.cloud_brain import CloudBrain

    async def fake_vision(messages, routing_info):
        routing_info["vision_images"] = vision_images
        return messages

    brain = CloudBrain.__new__(CloudBrain)
    brain._process_vision = fake_vision
    brain._mode_router = SimpleNamespace(route=lambda text: SimpleNamespace(mode="baseline", intensity="low", tags=[]))
    brain._is_greeting = lambda text: True  # skip RAG prefetch
    brain._get_unified_system_prompt = lambda *a, **k: "sys"
    brain._check_model_health = AsyncMock(return_value=True)
    brain._model_url = "http://fake:8080/v1"
    brain._model_name = "fake"
    brain._fallback_model_url = ""
    brain._get_all_tools = lambda: []
    brain._run_unified_loop = loop_mock
    brain._schedule_auto_learn = lambda messages: None
    brain._try_fast_path = fast_path_mock
    return brain


async def test_fast_path_skipped_on_photo_turn():
    loop_mock = AsyncMock(return_value="That's a red mug.")
    fast_path = AsyncMock(return_value=SimpleNamespace(handled=True, action="turn_off", entity_name="kitchen"))
    brain = _make_brain(loop_mock, fast_path, vision_images=1)
    messages = [
        {"role": "user", "content": "turn off the kitchen lights"},
        {"role": "assistant", "content": "Done!"},
        {"role": "user", "content": [_IMG]},
    ]

    with patch("orchestrator.cloud_brain.is_first_chat", return_value=False):
        await brain._chat_unified_inner(
            messages, False, None, object(), _last_user_text(messages), {"tool_calls": []}, False
        )

    fast_path.assert_not_awaited()
    loop_mock.assert_awaited_once()


async def test_fast_path_still_runs_on_text_turn():
    loop_mock = AsyncMock(return_value="unused")
    fast_path = AsyncMock(
        return_value=SimpleNamespace(handled=True, action="turn_off", entity_name="kitchen", response_text="Done!")
    )
    brain = _make_brain(loop_mock, fast_path, vision_images=0)
    messages = [{"role": "user", "content": "turn off the kitchen lights"}]

    with patch("orchestrator.cloud_brain.is_first_chat", return_value=False):
        await brain._chat_unified_inner(
            messages, False, None, object(), "turn off the kitchen lights", {"tool_calls": []}, False
        )

    fast_path.assert_awaited_once()
    loop_mock.assert_not_awaited()
