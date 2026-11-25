"""Photo turns from the dashboard chat (multi-part content).

Regression: `_last_user_text` used to skip list content and return the
PREVIOUS user turn, so sending a photo after "turn off the kitchen lights"
re-ran that Home Assistant command via the fast-path and never answered the
photo. Also: vision text appended to a photo turn is not the user's words, so
it must not drive the selfcare auto-log.

The end-to-end half (cloud_brain skips the HA fast path on a photo turn, and
still runs it on the same text without a photo) is in
test_image_parts_never_forwarded.py.
"""

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
