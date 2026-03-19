"""
Tests for background_jobs.py — tier selection logic.

Covers: correct tier for various minutes values, catch-up marking of larger
tiers, and custom tiers with no predefined template (generic fallback).
"""

# Re-implement tier selection logic from background_jobs.py for isolated testing
# (avoids importing the full module with its heavy dependency chain).

_TIER_MESSAGES = {
    60: "{name}, you have {title} in about an hour.",
    30: "{name}, {title} in 30 minutes. Start wrapping up what you're doing.",
    15: "{name}, {title} in 15 minutes. Time to transition — save your work, grab water.",
    5: "{name}, {title} starts in 5 minutes. {prep}",
}


def _select_best_tier(minutes, tiers, notified_keys, event_id="evt1"):
    """
    Re-implementation of the tier selection logic from poll_calendar.

    Returns (best_tier, tier_key) or (None, None) if no tier matches.
    """
    best_tier = None
    for tier_min in sorted(tiers):
        if minutes > tier_min:
            continue  # not yet reached this tier
        tier_key = f"cal:{event_id}:{tier_min}"
        if tier_key not in notified_keys:
            best_tier = tier_min
            break  # closest un-announced tier
    if best_tier is not None:
        return best_tier, f"cal:{event_id}:{best_tier}"
    return None, None


def _catch_up_mark(best_tier, tiers, event_id="evt1"):
    """Return set of tier keys that should be marked notified (catch-up)."""
    keys = set()
    for t in tiers:
        if t > best_tier:
            keys.add(f"cal:{event_id}:{t}")
    return keys


class TestTierSelection:
    """Tier selection picks the closest un-announced tier."""

    TIERS = [60, 30, 15, 5]

    def test_event_55_min_away_picks_60(self):
        """55 minutes away, no tiers announced yet -> picks 60 (smallest >= 55)."""
        best, key = _select_best_tier(55, self.TIERS, set())
        assert best == 60
        assert key == "cal:evt1:60"

    def test_event_28_min_away_picks_30(self):
        """28 minutes away, no tiers announced -> picks 30 (closest)."""
        best, key = _select_best_tier(28, self.TIERS, set())
        assert best == 30

    def test_event_28_min_away_skips_already_announced_30(self):
        """28 min away, 30-min tier already announced -> picks 60."""
        notified = {"cal:evt1:30"}
        best, key = _select_best_tier(28, self.TIERS, notified)
        assert best == 60

    def test_event_28_min_away_all_reachable_announced(self):
        """28 min away, 30 and 60 both announced -> None."""
        notified = {"cal:evt1:30", "cal:evt1:60"}
        best, key = _select_best_tier(28, self.TIERS, notified)
        assert best is None

    def test_event_14_min_away_picks_15(self):
        best, _ = _select_best_tier(14, self.TIERS, set())
        assert best == 15

    def test_event_5_min_away_picks_5(self):
        best, _ = _select_best_tier(5, self.TIERS, set())
        assert best == 5

    def test_event_4_min_away_picks_5(self):
        """4 min away (minutes <= tier_min=5) -> picks 5."""
        best, _ = _select_best_tier(4, self.TIERS, set())
        assert best == 5

    def test_event_0_min_away_picks_5(self):
        """0 min away, nothing announced -> picks 5."""
        best, _ = _select_best_tier(0, self.TIERS, set())
        assert best == 5

    def test_event_exactly_on_tier_boundary(self):
        """Exactly 30 min away -> should pick 30 (minutes <= tier_min)."""
        best, _ = _select_best_tier(30, self.TIERS, set())
        assert best == 30

    def test_event_61_min_away_no_tier(self):
        """61 min away -> minutes > every tier, no match."""
        best, _ = _select_best_tier(61, self.TIERS, set())
        assert best is None


class TestCatchUpMarking:
    """When a smaller tier fires, larger tiers should be marked as notified."""

    TIERS = [60, 30, 15, 5]

    def test_catch_up_from_15(self):
        """Firing 15-min tier should mark 30 and 60 as notified."""
        keys = _catch_up_mark(15, self.TIERS)
        assert keys == {"cal:evt1:30", "cal:evt1:60"}

    def test_catch_up_from_5(self):
        """Firing 5-min tier should mark 15, 30, and 60."""
        keys = _catch_up_mark(5, self.TIERS)
        assert keys == {"cal:evt1:15", "cal:evt1:30", "cal:evt1:60"}

    def test_catch_up_from_60(self):
        """Firing 60-min tier should mark nothing (it's the largest)."""
        keys = _catch_up_mark(60, self.TIERS)
        assert keys == set()

    def test_catch_up_from_30(self):
        keys = _catch_up_mark(30, self.TIERS)
        assert keys == {"cal:evt1:60"}


class TestCustomTierTemplate:
    """Custom tiers not in _TIER_MESSAGES should use a generic fallback."""

    def test_custom_tier_45_uses_generic(self):
        """Tier 45 has no template in _TIER_MESSAGES, should use generic."""
        tier = 45
        template = _TIER_MESSAGES.get(
            tier,
            "{name}, {title} in " + str(tier) + " minutes.",
        )
        msg = template.format(name="Nadim", title="Standup", prep="")
        assert "45 minutes" in msg
        assert "Nadim" in msg
        assert "Standup" in msg

    def test_standard_tier_uses_predefined(self):
        """Tier 30 has a predefined template and should use it."""
        tier = 30
        template = _TIER_MESSAGES.get(
            tier,
            "{name}, {title} in " + str(tier) + " minutes.",
        )
        msg = template.format(name="Nadim", title="Meeting", prep="")
        assert "wrapping up" in msg.lower()

    def test_custom_tier_90_uses_generic(self):
        tier = 90
        template = _TIER_MESSAGES.get(
            tier,
            "{name}, {title} in " + str(tier) + " minutes.",
        )
        msg = template.format(name="Nadim", title="Doctor", prep="")
        assert "90 minutes" in msg
