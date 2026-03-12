"""
Tests for mode_router.py — Intent classification.

Tests the deterministic v1 mode router: explicit overrides, emotional
intensity detection, curiosity detection, and default routing.
"""

import pytest
from mode_router import ModeRouter, RoutingResult


@pytest.fixture
def router():
    return ModeRouter()


# ── Explicit intent overrides ──

class TestExplicitOverrides:
    def test_mirror_analyze_me(self, router):
        result = router.route("Can you analyze me and my patterns?")
        assert result.mode == "mirror"

    def test_mirror_reflect_on(self, router):
        result = router.route("Reflect on my behavioral tendencies")
        assert result.mode == "mirror"

    def test_challenge_hold_me_accountable(self, router):
        result = router.route("Hold me accountable for this task")
        assert result.mode == "challenge"

    def test_challenge_push_me(self, router):
        result = router.route("I need you to push me today")
        assert result.mode == "challenge"

    def test_challenge_no_excuses(self, router):
        result = router.route("No excuses, tell me what to do")
        assert result.mode == "challenge"

    def test_challenge_dont_sugarcoat(self, router):
        result = router.route("Don't sugarcoat it, be honest with me")
        assert result.mode == "challenge"


# ── Emotional intensity ──

class TestEmotionalIntensity:
    def test_high_intensity_panic(self, router):
        result = router.route("I'm panicking and can't breathe")
        assert result.intensity == "high"
        assert result.mode == "baseline"

    def test_high_intensity_hopeless(self, router):
        result = router.route("Everything feels hopeless")
        assert result.intensity == "high"
        assert result.mode == "baseline"

    def test_high_intensity_falling_apart(self, router):
        result = router.route("I feel like I'm falling apart")
        assert result.intensity == "high"
        assert result.mode == "baseline"

    def test_medium_intensity_lonely(self, router):
        result = router.route("I've been feeling really lonely lately")
        assert result.intensity == "medium"
        assert result.mode == "counterbalance"

    def test_medium_intensity_spiraling(self, router):
        result = router.route("I'm spiraling again about work")
        assert result.intensity == "medium"
        assert result.mode == "counterbalance"

    def test_medium_intensity_overwhelmed(self, router):
        result = router.route("I'm so overwhelmed with everything")
        assert result.intensity == "medium"
        assert result.mode == "counterbalance"

    def test_low_intensity_neutral(self, router):
        result = router.route("What should I have for dinner?")
        assert result.intensity == "low"


# ── Curiosity detection ──

class TestCuriosityDetection:
    def test_how_does(self, router):
        result = router.route("How does ADHD medication work?")
        assert result.mode == "explainer"

    def test_what_is(self, router):
        result = router.route("What is the symbolism of Pisces?")
        assert result.mode == "explainer"

    def test_explain(self, router):
        result = router.route("Explain how neural networks learn")
        assert result.mode == "explainer"

    def test_difference_between(self, router):
        result = router.route("What's the difference between SSRIs and SNRIs?")
        assert result.mode == "explainer"

    def test_curiosity_overridden_by_medium_intensity(self, router):
        """Curiosity keywords shouldn't fire when intensity is medium+."""
        result = router.route("How does this spiral ever end?")
        # "spiral" triggers medium → counterbalance, even though "how does" is curiosity
        assert result.intensity == "medium"
        assert result.mode == "counterbalance"


# ── Default routing ──

class TestDefaultRouting:
    def test_default_low_is_explainer(self, router):
        result = router.route("Turn on the bedroom lights")
        assert result.mode == "explainer"
        assert result.intensity == "low"

    def test_empty_input(self, router):
        result = router.route("")
        assert result.mode == "explainer"
        assert result.intensity == "low"

    def test_long_input(self, router):
        text = "This is a really long message. " * 200
        result = router.route(text)
        assert result.mode in ("explainer", "mirror", "counterbalance", "challenge", "baseline")
        assert result.intensity in ("low", "medium", "high")


# ── Tags ──

class TestTags:
    def test_explicit_tag_present(self, router):
        result = router.route("Hold me accountable")
        assert any("explicit:" in t for t in result.tags)

    def test_intensity_tag_present(self, router):
        result = router.route("I feel so ashamed right now")
        assert any("intensity_medium:" in t for t in result.tags)

    def test_curiosity_tag_present(self, router):
        result = router.route("Tell me about Pisces traits")
        assert any("curiosity:" in t for t in result.tags)


# ── RoutingResult structure ──

class TestRoutingResult:
    def test_dataclass_fields(self, router):
        result = router.route("hello")
        assert hasattr(result, "mode")
        assert hasattr(result, "intensity")
        assert hasattr(result, "tags")
        assert isinstance(result.tags, list)
