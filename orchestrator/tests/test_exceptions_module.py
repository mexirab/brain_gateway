"""Tests for orchestrator/exceptions.py — the parts the code relies on.

tool_registry.execute_tool catches TransientError to answer "temporarily
unavailable", and ExternalServiceError is the TransientError the integrations
raise. (ConfigError / ToolError / ToolNotFoundError are not raised anywhere in
orchestrator/, so a class-statement restatement test for them pins nothing.)
"""

import pytest

from orchestrator.exceptions import BrainGatewayError, ExternalServiceError, TransientError


@pytest.mark.parametrize(
    "message, text",
    [("API returned 503", "[calendar] API returned 503"), ("", "[calendar] Service error")],
    ids=["with-message", "default-message"],
)
def test_external_service_error_is_a_transient_error_naming_its_service(message, text):
    with pytest.raises(TransientError) as info:
        raise ExternalServiceError("calendar", message)
    assert isinstance(info.value, BrainGatewayError)
    assert info.value.service == "calendar"
    assert str(info.value) == text
