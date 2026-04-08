"""
Exception hierarchy for Brain Gateway.

Provides typed exceptions so error handling and metrics can distinguish
between transient failures (retry), config issues (fix and redeploy),
and tool execution errors.

Usage:
    from exceptions import TransientError, ExternalServiceError, ToolNotFoundError

    raise ExternalServiceError("calendar", "Google Calendar API returned 503")
    raise ToolNotFoundError("unknown_tool")
"""


class BrainGatewayError(Exception):
    """Base exception for all Brain Gateway errors."""


# -- Configuration errors (not retryable) ------------------------------------


class ConfigError(BrainGatewayError):
    """Missing or invalid configuration. Fix .env / user_profile and restart."""


# -- Transient / external errors (retryable) ---------------------------------


class TransientError(BrainGatewayError):
    """Transient failure — network timeout, service temporarily down, etc."""


class ExternalServiceError(TransientError):
    """Failure in an external service (HA, Calendar, Gmail, YNAB, etc.)."""

    def __init__(self, service: str, message: str = ""):
        self.service = service
        super().__init__(f"[{service}] {message}" if message else f"[{service}] Service error")


# -- Tool errors -------------------------------------------------------------


class ToolError(BrainGatewayError):
    """Error during tool execution."""


class ToolNotFoundError(ToolError):
    """Unknown tool name requested."""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        super().__init__(f"Unknown tool: {tool_name}")
