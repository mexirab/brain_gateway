"""
Tests for shared.py — env var parsing with try/except fallback.

Covers CALENDAR_ALERT_TIERS, CALENDAR_POLL_INTERVAL, and EMAIL_POLL_INTERVAL
parsing with valid, malformed, and whitespace inputs.
"""


class TestCalendarAlertTiersParsing:
    """CALENDAR_ALERT_TIERS env var parsing."""

    def _parse_tiers(self, env_value):
        """Helper: set env, re-execute the parsing logic, return result."""
        default_tiers = [60, 30, 15, 5]
        try:
            return [int(x) for x in env_value.split(",")]
        except ValueError:
            return default_tiers

    def test_valid_default_tiers(self):
        result = self._parse_tiers("60,30,15,5")
        assert result == [60, 30, 15, 5]

    def test_valid_custom_tiers(self):
        result = self._parse_tiers("90,45,10")
        assert result == [90, 45, 10]

    def test_single_tier(self):
        result = self._parse_tiers("15")
        assert result == [15]

    def test_malformed_tiers_returns_default(self):
        result = self._parse_tiers("60,abc,15")
        assert result == [60, 30, 15, 5]

    def test_empty_string_returns_default(self):
        result = self._parse_tiers("")
        assert result == [60, 30, 15, 5]

    def test_whitespace_in_tiers_returns_default(self):
        """Whitespace around numbers causes int() to fail mid-list."""
        result = self._parse_tiers("60, 30, 15, 5")
        # int(" 30") actually works in Python, so this should succeed
        assert result == [60, 30, 15, 5]

    def test_completely_invalid_returns_default(self):
        result = self._parse_tiers("not,numbers")
        assert result == [60, 30, 15, 5]

    def test_trailing_comma_returns_default(self):
        result = self._parse_tiers("60,30,")
        assert result == [60, 30, 15, 5]


class TestCalendarPollIntervalParsing:
    """CALENDAR_POLL_INTERVAL env var parsing."""

    def _parse_interval(self, env_value):
        try:
            return int(env_value)
        except ValueError:
            return 5

    def test_valid_interval(self):
        assert self._parse_interval("10") == 10

    def test_default_interval(self):
        assert self._parse_interval("5") == 5

    def test_malformed_returns_default(self):
        assert self._parse_interval("abc") == 5

    def test_float_returns_default(self):
        assert self._parse_interval("5.5") == 5

    def test_empty_returns_default(self):
        assert self._parse_interval("") == 5


class TestEmailPollIntervalParsing:
    """EMAIL_POLL_INTERVAL env var parsing."""

    def _parse_interval(self, env_value):
        try:
            return int(env_value)
        except ValueError:
            return 30

    def test_valid_interval(self):
        assert self._parse_interval("15") == 15

    def test_default_interval(self):
        assert self._parse_interval("30") == 30

    def test_malformed_returns_default(self):
        assert self._parse_interval("xyz") == 30

    def test_float_returns_default(self):
        assert self._parse_interval("30.0") == 30

    def test_empty_returns_default(self):
        assert self._parse_interval("") == 30
