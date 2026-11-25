"""Tests for orchestrator/config.py — Settings defaults, coercion and validators.

Every Settings here comes from ``make_settings`` (fixtures_memory.py): built
from exactly the given env, with no host env and no deployed /app/.env, and
without reloading the module (the shared ``settings`` singleton stays intact).
"""

import pytest

from orchestrator.tests.fixtures_memory import make_settings


def test_defaults():
    s = make_settings()
    assert s.max_tool_rounds == 5
    assert s.selfcare_enabled is True
    assert s.vision_enabled is False
    assert s.ha_url == ""
    assert s.tts_url == ""
    assert s.fallback_model_url == ""


@pytest.mark.parametrize(
    "env,attr,expected",
    [
        pytest.param({"MAX_TOOL_ROUNDS": "10"}, "max_tool_rounds", 10, id="int"),
        pytest.param({"MIN_COS": "0.55"}, "min_cos", pytest.approx(0.55), id="float"),
        pytest.param({"SELFCARE_ENABLED": "false"}, "selfcare_enabled", False, id="bool"),
        pytest.param({"CALENDAR_POLL_INTERVAL": "0"}, "calendar_poll_interval", 1, id="positive-int-clamps-zero"),
        pytest.param({"CALENDAR_POLL_INTERVAL": "15"}, "calendar_poll_interval", 15, id="positive-int-valid"),
    ],
)
def test_type_coercion(env, attr, expected):
    value = getattr(make_settings(**env), attr)
    assert value == expected
    if isinstance(expected, int):  # bool included: coerced, not just equal
        assert type(value) is type(expected)


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param(None, [60, 30, 15, 5], id="default"),
        pytest.param("90,45,10", [90, 45, 10], id="custom"),
        pytest.param("bad,data", [60, 30, 15, 5], id="invalid-falls-back"),
        pytest.param("5", [5], id="single"),
    ],
)
def test_alert_tiers(raw, expected):
    env = {} if raw is None else {"CALENDAR_ALERT_TIERS": raw}
    assert make_settings(**env).alert_tiers == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param(None, [], id="not-set"),
        pytest.param("", [], id="empty"),
        pytest.param("http://pi.hole:8053", ["http://pi.hole:8053"], id="single"),
        pytest.param(
            "http://10.0.0.248:8053, http://10.0.0.58:8053",
            ["http://10.0.0.248:8053", "http://10.0.0.58:8053"],
            id="multiple",
        ),
        pytest.param("http://a:8053,", ["http://a:8053"], id="trailing-comma"),
    ],
)
def test_pihole_url_list(raw, expected):
    env = {} if raw is None else {"PIHOLE_URLS": raw}
    assert make_settings(**env).pihole_url_list == expected


class TestSelfAuditInvestigatorConfig:
    """validate_self_audit_config: clamps for the investigator/webhook settings
    and the short-webhook-token drop (generate-configs.sh mirrors the 24-char
    rule, so the two must agree)."""

    BASE = {"SELF_AUDIT_ENABLED": "true", "SELF_AUDIT_LOKI_URL": "http://loki:3100"}

    def _make_settings(self, env_overrides):
        return make_settings(**self.BASE, **env_overrides)

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

    @pytest.mark.parametrize(
        "length,kept", [pytest.param(23, False, id="23-dropped"), pytest.param(24, True, id="24-kept")]
    )
    def test_webhook_token_minimum_length(self, length, kept):
        s = self._make_settings({"SELF_AUDIT_WEBHOOK_TOKEN": "x" * length})
        assert s.self_audit_webhook_token == ("x" * length if kept else "")


class TestVoiceThinkingConfig:
    """VOICE_THINKING_ENABLED: default off (the pre-switch voice payload), opt-in
    via env, re-exported as shared.VOICE_THINKING_ENABLED (read per voice turn
    by unified_loop)."""

    def test_default_is_off(self):
        assert make_settings().voice_thinking_enabled is False

    @pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("false", False), ("0", False)])
    def test_env_coercion(self, raw, expected):
        assert make_settings(VOICE_THINKING_ENABLED=raw).voice_thinking_enabled is expected

    def test_shared_reexports_setting(self):
        from orchestrator import shared
        from orchestrator.config import settings

        assert isinstance(shared.VOICE_THINKING_ENABLED, bool)
        assert shared.VOICE_THINKING_ENABLED is settings.voice_thinking_enabled
