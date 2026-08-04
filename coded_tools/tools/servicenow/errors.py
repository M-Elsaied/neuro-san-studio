"""
Exception taxonomy for the ServiceNow tool family.

Every exception carries a short, stable ``reason`` code so that audit markers and
tests can assert on the reason rather than on prose, and so the text handed to an
LLM never has to be parsed.
"""

from typing import Any
from typing import Dict
from typing import Optional


class ServiceNowError(Exception):
    """Base for every error raised inside this package."""

    reason: str = "servicenow_error"

    def __init__(self, message: str, reason: Optional[str] = None, **details: Any):
        super().__init__(message)
        self.message: str = message
        if reason is not None:
            self.reason = reason
        self.details: Dict[str, Any] = details

    def as_dict(self) -> Dict[str, Any]:
        """
        :return: A JSON-serializable dictionary describing the error.
        """
        payload: Dict[str, Any] = {"error": self.message, "reason": self.reason}
        payload.update(self.details)
        return payload


class ProfileError(ServiceNowError):
    """The deployment profile is missing, malformed, or internally inconsistent."""

    reason = "profile_invalid"


class PolicyDenied(ServiceNowError):
    """
    The requested operation, entity or field is not on an allow-list.

    This is the infosec signal: it means an agent asked for something the
    deployment has not sanctioned, which is materially different from a
    downstream failure.
    """

    reason = "policy_denied"


class GateError(ServiceNowError):
    """A gated write was attempted without a valid approval token."""

    reason = "not_approved"


class AuthError(ServiceNowError):
    """A token could not be obtained from the authorization server."""

    reason = "auth_failed"


class TransportError(ServiceNowError):
    """The downstream call failed at the transport or HTTP-status level."""

    reason = "downstream_failed"


class CircuitOpenError(TransportError):
    """The circuit breaker for this route is open; the call was not attempted."""

    reason = "circuit_open"
