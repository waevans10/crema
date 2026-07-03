"""Structured error handling for Gaggimate MCP server."""

from enum import Enum
from typing import Optional


class ErrorCode(Enum):
    """Error codes for categorizing failures."""

    # Client errors (4xx equivalent)
    INVALID_INPUT = "invalid_input"
    PROFILE_NOT_FOUND = "profile_not_found"
    SHOT_NOT_FOUND = "shot_not_found"
    UNAUTHORIZED = "unauthorized"
    SAFETY_LIMIT_EXCEEDED = "safety_limit_exceeded"

    # Server errors (5xx equivalent)
    API_ERROR = "api_error"
    PARSE_ERROR = "parse_error"
    TIMEOUT = "timeout"

    # External errors
    DEVICE_UNREACHABLE = "device_unreachable"
    WEBSOCKET_ERROR = "websocket_error"


class GaggimateError(Exception):
    """
    Structured exception for Gaggimate MCP server errors.

    Attributes:
        code: Error code for categorization
        message: Human-readable error message
        details: Optional additional context
        retryable: Whether the operation can be retried
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Optional[dict] = None,
        retryable: bool = False,
    ):
        """
        Initialize GaggimateError.

        Args:
            code: Error code from ErrorCode enum
            message: Human-readable error message
            details: Optional dictionary with additional context
            retryable: Whether this error indicates a retryable failure
        """
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
        super().__init__(message)

    def to_dict(self) -> dict:
        """
        Serialize error to dictionary for JSON responses.

        Returns:
            Dictionary representation suitable for MCP responses
        """
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details or {},
                "retryable": self.retryable,
            }
        }

    def __str__(self) -> str:
        """String representation of error."""
        return f"[{self.code.value}] {self.message}"

    def __repr__(self) -> str:
        """Developer-friendly representation."""
        return (
            f"GaggimateError(code={self.code}, message={self.message!r}, "
            f"details={self.details}, retryable={self.retryable})"
        )
