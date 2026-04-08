"""Tests for orchestrator/exceptions.py — exception hierarchy and attributes."""

import importlib
import os

# Force import from orchestrator dir (not stdlib 'exceptions')
_mod_path = os.path.join(os.path.dirname(__file__), "..", "exceptions.py")
_spec = importlib.util.spec_from_file_location("exceptions", _mod_path)
_exceptions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_exceptions)

BrainGatewayError = _exceptions.BrainGatewayError
ConfigError = _exceptions.ConfigError
ExternalServiceError = _exceptions.ExternalServiceError
ToolError = _exceptions.ToolError
ToolNotFoundError = _exceptions.ToolNotFoundError
TransientError = _exceptions.TransientError


class TestHierarchy:
    """All exceptions descend from BrainGatewayError."""

    def test_config_error_is_brain_gateway_error(self):
        assert issubclass(ConfigError, BrainGatewayError)

    def test_transient_error_is_brain_gateway_error(self):
        assert issubclass(TransientError, BrainGatewayError)

    def test_external_service_error_is_transient(self):
        assert issubclass(ExternalServiceError, TransientError)

    def test_external_service_error_is_brain_gateway_error(self):
        assert issubclass(ExternalServiceError, BrainGatewayError)

    def test_tool_error_is_brain_gateway_error(self):
        assert issubclass(ToolError, BrainGatewayError)

    def test_tool_not_found_is_tool_error(self):
        assert issubclass(ToolNotFoundError, ToolError)

    def test_tool_not_found_is_brain_gateway_error(self):
        assert issubclass(ToolNotFoundError, BrainGatewayError)


class TestIsinstance:
    """isinstance checks work with raised exceptions."""

    def test_catch_external_as_transient(self):
        try:
            raise ExternalServiceError("calendar", "503")
        except TransientError:
            pass  # Should be caught

    def test_catch_external_as_brain_gateway(self):
        try:
            raise ExternalServiceError("gmail", "timeout")
        except BrainGatewayError:
            pass  # Should be caught

    def test_catch_tool_not_found_as_tool_error(self):
        try:
            raise ToolNotFoundError("bad_tool")
        except ToolError:
            pass  # Should be caught

    def test_config_error_not_transient(self):
        assert not issubclass(ConfigError, TransientError)


class TestExternalServiceError:
    """ExternalServiceError stores .service attribute."""

    def test_service_attribute(self):
        e = ExternalServiceError("calendar", "API returned 503")
        assert e.service == "calendar"

    def test_message_format_with_message(self):
        e = ExternalServiceError("gmail", "rate limited")
        assert "[gmail]" in str(e)
        assert "rate limited" in str(e)

    def test_message_format_without_message(self):
        e = ExternalServiceError("ha")
        assert "[ha]" in str(e)
        assert "Service error" in str(e)

    def test_service_preserved_after_catch(self):
        try:
            raise ExternalServiceError("ynab", "401 Unauthorized")
        except TransientError as e:
            assert hasattr(e, "service")
            assert e.service == "ynab"


class TestToolNotFoundError:
    """ToolNotFoundError stores .tool_name attribute."""

    def test_tool_name_attribute(self):
        e = ToolNotFoundError("unknown_tool")
        assert e.tool_name == "unknown_tool"

    def test_message_contains_tool_name(self):
        e = ToolNotFoundError("bad_tool")
        assert "bad_tool" in str(e)

    def test_tool_name_preserved_after_catch(self):
        try:
            raise ToolNotFoundError("missing")
        except ToolError as e:
            assert hasattr(e, "tool_name")
            assert e.tool_name == "missing"
