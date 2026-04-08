"""Tests for orchestrator/config.py — Settings class."""

import os
from unittest.mock import patch

import pytest


class TestSettingsDefaults:
    """Settings loads with sane defaults when no env vars are set."""

    def _make_settings(self, env_overrides=None):
        """Create a Settings instance with optional env overrides."""
        env = {
            "MODEL_URL": "http://localhost:8080/v1",
            "MODEL_NAME": "test-model",
        }
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=False):
            # Import inside to pick up patched env
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings()

    def test_default_model_url(self):
        s = self._make_settings()
        assert s.model_url == "http://localhost:8080/v1"

    def test_default_max_tool_rounds(self):
        s = self._make_settings()
        assert s.max_tool_rounds == 5

    def test_default_booleans(self):
        s = self._make_settings()
        assert s.selfcare_enabled is True
        assert s.snapcast_enabled is False
        assert s.vision_enabled is False

    def test_default_empty_strings(self):
        s = self._make_settings()
        assert s.ha_url == ""
        assert s.tts_url == ""
        assert s.fallback_model_url == ""


class TestTypeCoercion:
    """Pydantic coerces string env vars to typed fields."""

    def _make_settings(self, env_overrides):
        env = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m"}
        env.update(env_overrides)
        with patch.dict(os.environ, env, clear=False):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings()

    def test_int_from_string(self):
        s = self._make_settings({"MAX_TOOL_ROUNDS": "10"})
        assert s.max_tool_rounds == 10
        assert isinstance(s.max_tool_rounds, int)

    def test_float_from_string(self):
        s = self._make_settings({"MIN_COS": "0.55"})
        assert s.min_cos == pytest.approx(0.55)

    def test_bool_from_string(self):
        s = self._make_settings({"SELFCARE_ENABLED": "false"})
        assert s.selfcare_enabled is False

    def test_positive_int_validator_clamps_zero(self):
        s = self._make_settings({"CALENDAR_POLL_INTERVAL": "0"})
        assert s.calendar_poll_interval == 1

    def test_positive_int_validator_allows_valid(self):
        s = self._make_settings({"CALENDAR_POLL_INTERVAL": "15"})
        assert s.calendar_poll_interval == 15


class TestAlertTiers:
    """alert_tiers property parses comma-separated string into list[int]."""

    def _make_settings(self, env_overrides=None):
        env = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m"}
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=False):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings()

    def test_default_tiers(self):
        s = self._make_settings()
        assert s.alert_tiers == [60, 30, 15, 5]

    def test_custom_tiers(self):
        s = self._make_settings({"CALENDAR_ALERT_TIERS": "90,45,10"})
        assert s.alert_tiers == [90, 45, 10]

    def test_invalid_tiers_returns_fallback(self):
        s = self._make_settings({"CALENDAR_ALERT_TIERS": "bad,data"})
        assert s.alert_tiers == [60, 30, 15, 5]

    def test_single_tier(self):
        s = self._make_settings({"CALENDAR_ALERT_TIERS": "5"})
        assert s.alert_tiers == [5]


class TestPiholeUrlList:
    """pihole_url_list property splits comma-separated URLs."""

    def _make_settings(self, env_overrides=None):
        env = {"MODEL_URL": "http://localhost:8080/v1", "MODEL_NAME": "m"}
        if env_overrides:
            env.update(env_overrides)
        with patch.dict(os.environ, env, clear=False):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings()

    def test_empty_returns_empty_list(self):
        s = self._make_settings({"PIHOLE_URLS": ""})
        assert s.pihole_url_list == []

    def test_single_url(self):
        s = self._make_settings({"PIHOLE_URLS": "http://pi.hole:8053"})
        assert s.pihole_url_list == ["http://pi.hole:8053"]

    def test_multiple_urls(self):
        s = self._make_settings({"PIHOLE_URLS": "http://10.0.0.248:8053, http://10.0.0.58:8053"})
        assert s.pihole_url_list == ["http://10.0.0.248:8053", "http://10.0.0.58:8053"]

    def test_trailing_comma_ignored(self):
        s = self._make_settings({"PIHOLE_URLS": "http://a:8053,"})
        assert s.pihole_url_list == ["http://a:8053"]

    def test_not_set_returns_empty(self):
        s = self._make_settings()
        assert s.pihole_url_list == []
