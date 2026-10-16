"""Typed errors mapped onto a stable JSON error contract.

Every failure the service can surface — invalid UID, upstream risk control,
rate limiting, CAPTCHA challenge, IP blocking — is modelled here so that the
API returns a predictable ``{"error": {...}}`` body with a machine-readable
``code`` instead of leaking upstream internals.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AniimoError(Exception):
    """Base application error.

    Attributes:
        code: stable machine code (SCREAMING_SNAKE_CASE).
        message: human-readable summary (safe to show a client).
        http_status: HTTP status to return.
        retry_after: optional seconds the caller should wait before retrying.
        details: optional structured context.
    """

    code: str = "INTERNAL_ERROR"
    message: str = "Unexpected error"
    http_status: int = 500
    retry_after: int | None = None
    details: dict | None = None

    def to_dict(self) -> dict:
        body: dict = {"code": self.code, "message": self.message}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        if self.details:
            body["details"] = self.details
        return {"error": body}


# --- Client-facing validation / lookup -------------------------------------
class InvalidUidError(AniimoError):
    def __init__(self, uid: str, server: str | None = None):
        super().__init__(
            code="UID_NOT_FOUND",
            message="No character found for the given UID/server.",
            http_status=404,
            details={"uid": uid, "server": server},
        )


class InvalidServerError(AniimoError):
    def __init__(self, server: str):
        super().__init__(
            code="SERVER_INVALID",
            message="Unknown or unsupported server.",
            http_status=422,
            details={"server": server},
        )


# --- Anti-abuse / upstream protections -------------------------------------
class RateLimitedError(AniimoError):
    def __init__(self, retry_after: int = 60, *, upstream: bool = False):
        super().__init__(
            code="RATE_LIMITED_UPSTREAM" if upstream else "RATE_LIMITED",
            message="Too many requests. Slow down and retry later.",
            http_status=429,
            retry_after=retry_after,
        )


class CaptchaRequiredError(AniimoError):
    def __init__(self):
        super().__init__(
            code="CAPTCHA_REQUIRED",
            message="Upstream demanded a CAPTCHA/risk challenge; request could not be completed automatically.",
            http_status=423,
        )


class IpBlockedError(AniimoError):
    def __init__(self):
        super().__init__(
            code="IP_BLOCKED",
            message="Upstream rejected this IP (geo/risk block). Route through an allowed egress.",
            http_status=403,
        )


class RiskControlError(AniimoError):
    def __init__(self, upstream_code: int | str, message: str | None = None):
        super().__init__(
            code="RISK_CONTROL_BLOCK",
            message=message or "Order blocked by upstream risk control.",
            http_status=409,
            details={"upstream_code": upstream_code},
        )


class UpstreamUnavailableError(AniimoError):
    def __init__(self, message: str = "Upstream temporarily unavailable."):
        super().__init__(
            code="UPSTREAM_UNAVAILABLE",
            message=message,
            http_status=502,
        )


class UpstreamBusinessError(AniimoError):
    """Any non-zero business ``code`` from the upstream JSON envelope."""

    def __init__(self, upstream_code: int | str, message: str | None = None):
        super().__init__(
            code="UPSTREAM_BUSINESS_ERROR",
            message=message or f"Upstream returned business code {upstream_code}.",
            http_status=502,
            details={"upstream_code": upstream_code},
        )
