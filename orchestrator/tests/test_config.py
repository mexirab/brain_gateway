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
        # Full isolation from a configured host box (these "defaults" tests run
        # in the orchestrator container, which both exports every compose var
        # AND ships /app/.env):
        #   - clear=True       → no host process env leaks in
        #   - patch dotenv     → config.py's module-scope load_dotenv("/app/.env")
        #                        is a no-op on reload, so .env keys aren't pushed
        #                        back into the cleared environ
        #   - _env_file=None   → Settings() itself doesn't re-read /app/.env
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
            # Import inside to pick up patched env
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings(_env_file=None)

    def test_default_model_url(self):
        s = self._make_settings()
        assert s.model_url == "http://localhost:8080/v1"

    def test_default_max_tool_rounds(self):
        s = self._make_settings()
        assert s.max_tool_rounds == 5

    def test_default_booleans(self):
        s = self._make_settings()
        assert s.selfcare_enabled is True
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
        # Isolate from host process env + the deployed /app/.env (see the
        # TestSettingsDefaults._make_settings comment for the three-part guard).
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings(_env_file=None)

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
        # Isolate from host process env + the deployed /app/.env (see the
        # TestSettingsDefaults._make_settings comment for the three-part guard).
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings(_env_file=None)

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
        # Isolate from host process env + the deployed /app/.env (see the
        # TestSettingsDefaults._make_settings comment for the three-part guard).
        with patch.dict(os.environ, env, clear=True), patch("dotenv.load_dotenv"):
            from importlib import import_module, reload

            mod = import_module("config")
            reload(mod)
            return mod.Settings(_env_file=None)

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


class TestSelfAuditInvestigatorConfig:
    """validate_self_audit_config: clamps for the investigator/webhook settings
    and the short-webhook-token drop (generate-configs.sh mirrors the 24-char
    rule, so the two must agree)."""

    def _make_settings(self, env_overrides):
        env = {
            "MODEL_URL": "http://localhost:8080/v1",
            "MODEL_NAME": "m",
            "SELF_AUDIT_ENABLED": "true",
            "SELF_AUDIT_LOKI_URL": "http://loki:3100",
        }
        env.update(env_overrides)
        # Instantiate Settings directly — no module reload, so the shared
        # `settings` singleton other tests hold is never replaced. Importing
        # as orchestrator.config also works in the image layout, where a bare
        # `import config` resolves to the /app/config directory.
        from orchestrator.config import Settings

        with patch.dict(os.environ, env, clear=True):
            return Settings(_env_file=None)

    def test_defaults_are_inside_the_clamps(self):
        s = self._make_settings({})
        assert s.self_audit_investigate is False
        assert s.self_audit_loki_limit_per_group == 1500
        assert s.self_audit_alert_lookback_hours == 3
        assert s.self_audit_investigate_max_rounds == 12
        assert s.self_audit_investigate_max_tool_calls == 30
        assert s.self_audit_investigate_max_seconds == 600
        assert s.self_audit_webhook_token == ""

    @pytest.mark.parametrize(
        "env_key,attr,low_in,low_out,high_in,high_out",
        [
            ("SELF_AUDIT_LOKI_LIMIT_PER_GROUP", "self_audit_loki_limit_per_group", "1", 50, "99999", 5000),
            ("SELF_AUDIT_ALERT_LOOKBACK_HOURS", "self_audit_alert_lookback_hours", "0", 1, "100", 24),
            ("SELF_AUDIT_INVESTIGATE_MAX_ROUNDS", "self_audit_investigate_max_rounds", "-3", 2, "500", 30),
            ("SELF_AUDIT_INVESTIGATE_MAX_TOOL_CALLS", "self_audit_investigate_max_tool_calls", "0", 1, "1000", 80),
            ("SELF_AUDIT_INVESTIGATE_MAX_SECONDS", "self_audit_investigate_max_seconds", "5", 60, "86400", 1800),
        ],
    )
    def test_clamps(self, env_key, attr, low_in, low_out, high_in, high_out):
        assert getattr(self._make_settings({env_key: low_in}), attr) == low_out
        assert getattr(self._make_settings({env_key: high_in}), attr) == high_out

    def test_in_range_value_is_kept(self):
        s = self._make_settings({"SELF_AUDIT_INVESTIGATE_MAX_SECONDS": "900"})
        assert s.self_audit_investigate_max_seconds == 900

    def test_short_webhook_token_is_dropped(self):
        s = self._make_settings({"SELF_AUDIT_WEBHOOK_TOKEN": "x" * 23})
        assert s.self_audit_webhook_token == ""

    def test_webhook_token_of_24_chars_is_kept(self):
        s = self._make_settings({"SELF_AUDIT_WEBHOOK_TOKEN": "x" * 24})
        assert s.self_audit_webhook_token == "x" * 24
