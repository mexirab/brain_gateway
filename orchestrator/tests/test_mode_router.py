"""
Tests for mode_router.py — Intent classification.

Tests the deterministic v1 mode router: explicit overrides, emotional
intensity detection, curiosity detection, and default routing. Uses
conftest's ``mode_router`` fixture.
"""

import pytest


@pytest.mark.parametrize(
    "text, mode, intensity",
    [
        # Explicit intent overrides
        pytest.param("Can you analyze me and my patterns?", "mirror", None, id="analyze-me"),
        pytest.param("Reflect on my behavioral tendencies", "mirror", None, id="reflect-on"),
        pytest.param("Hold me accountable for this task", "challenge", None, id="hold-me-accountable"),
        pytest.param("I need you to push me today", "challenge", None, id="push-me"),
        pytest.param("No excuses, tell me what to do", "challenge", None, id="no-excuses"),
        pytest.param("Don't sugarcoat it, be honest with me", "challenge", None, id="dont-sugarcoat"),
        # Emotional intensity
        pytest.param("I'm panicking and can't breathe", "baseline", "high", id="panic"),
        pytest.param("Everything feels hopeless", "baseline", "high", id="hopeless"),
        pytest.param("I feel like I'm falling apart", "baseline", "high", id="falling-apart"),
        pytest.param("I've been feeling really lonely lately", "counterbalance", "medium", id="lonely"),
        pytest.param("I'm spiraling again about work", "counterbalance", "medium", id="spiraling"),
        pytest.param("I'm so overwhelmed with everything", "counterbalance", "medium", id="overwhelmed"),
        pytest.param("What should I have for dinner?", None, "low", id="neutral"),
        # Curiosity
        pytest.param("How does ADHD medication work?", "explainer", None, id="how-does"),
        pytest.param("What is the symbolism of Pisces?", "explainer", None, id="what-is"),
        pytest.param("Explain how neural networks learn", "explainer", None, id="explain"),
        pytest.param("What's the difference between SSRIs and SNRIs?", "explainer", None, id="difference-between"),
        # "spiral" is medium intensity, which beats the "how does" curiosity keyword.
        pytest.param("How does this spiral ever end?", "counterbalance", "medium", id="intensity-beats-curiosity"),
        # Default routing
        pytest.param("Turn on the bedroom lights", "explainer", "low", id="default-low"),
        pytest.param("", "explainer", "low", id="empty"),
    ],
)
def test_route(mode_router, text, mode, intensity):
    result = mode_router.route(text)
    if mode is not None:
        assert result.mode == mode
    if intensity is not None:
        assert result.intensity == intensity
    assert isinstance(result.tags, list)


def test_long_input_routes_without_error(mode_router):
    result = mode_router.route("This is a really long message. " * 200)
    assert result.mode in ("explainer", "mirror", "counterbalance", "challenge", "baseline")
    assert result.intensity in ("low", "medium", "high")


@pytest.mark.parametrize(
    "text, tag_prefix",
    [
        pytest.param("Hold me accountable", "explicit:", id="explicit"),
        pytest.param("I feel so ashamed right now", "intensity_medium:", id="intensity"),
        pytest.param("Tell me about Pisces traits", "curiosity:", id="curiosity"),
    ],
)
def test_tags_name_the_rule_that_fired(mode_router, text, tag_prefix):
    assert any(tag_prefix in t for t in mode_router.route(text).tags)
