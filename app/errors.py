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


class ProductNotFoundError(AniimoError):
    """The requested package_id is not in the live catalogue (vs a bad server)."""

    def __init__(self, package_id: str):
        super().__init__(
            code="PRODUCT_NOT_FOUND",
            message="No package with that id in the current catalogue.",
            http_status=404,
            details={"package_id": package_id},
        )


class RegionCurrencyMismatchError(AniimoError):
    def __init__(self, expected: str, got: str):
        super().__init__(
            code="REGION_CURRENCY_MISMATCH",
            message=f"Upstream catalogue is priced in {got}, not {expected}. "
            "Route egress through a Taiwan IP.",
            http_status=409,
            details={"expected": expected, "got": got},
        )


class PaymentChannelUnavailableError(AniimoError):
    def __init__(self, channel: str):
        super().__init__(
            code="PAYMENT_CHANNEL_UNAVAILABLE",
            message=f"Upstream does not offer the '{channel}' payment channel for this context.",
            http_status=409,
            details={"channel": channel},
        )


class UpstreamInvalidResponseError(AniimoError):
    """place_order returned 'ok' but without a usable order per the contract."""

    def __init__(self, reason: str, raw: dict | None = None):
        super().__init__(
            code="UPSTREAM_INVALID_RESPONSE",
            message=f"Upstream order response did not satisfy the contract: {reason}",
            http_status=502,
            details={"reason": reason, **({"raw_keys": sorted(raw)} if raw else {})},
        )


class OrderResultUnknownError(AniimoError):
    """We sent place_order but never learned the outcome (e.g. read timeout).

    The caller must NOT blindly retry; it should query order status first.
    """

    def __init__(self, idempotency_key: str | None = None):
        super().__init__(
            code="ORDER_RESULT_UNKNOWN",
            message="Order was submitted but its result is unconfirmed. "
            "Check order status before retrying — do not resend.",
            http_status=202,
            details={"idempotency_key": idempotency_key} if idempotency_key else None,
        )


class DuplicateOrderError(AniimoError):
    """The same order request is already in flight / was just placed."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            code="DUPLICATE_ORDER",
            message="An identical order is already in progress or was just created.",
            http_status=409,
            details={"idempotency_key": idempotency_key},
        )


class IdempotencyKeyConflictError(AniimoError):
    """The idempotency key was reused with different order data."""

    def __init__(self, idempotency_key: str):
        super().__init__(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="This idempotency key was already used for a different order.",
            http_status=409,
            details={"idempotency_key": idempotency_key},
        )


class MockDataInLiveModeError(AniimoError):
    """A live-mode flow received mock-sourced character data."""

    def __init__(self):
        super().__init__(
            code="MOCK_DATA_IN_LIVE_MODE",
            message="Refusing to place a live order from mock character data.",
            http_status=409,
        )


class AccountAuthenticationError(AniimoError):
    """The operator's upstream credential is missing, expired or rejected."""

    def __init__(self):
        super().__init__(
            code="ACCOUNT_AUTH_REQUIRED",
            message="Operator account authentication was rejected or expired. Renew the server-side credential.",
            http_status=502,
        )


class UidNotInAccountError(InvalidUidError):
    """Account-scoped lookup cannot establish whether the UID exists globally."""

    def __init__(self, uid: str, server: str):
        super().__init__(uid, server)
        self.code = "UID_NOT_IN_ACCOUNT"
        self.message = "UID/server is not among the connected account's roles. Global existence is not verified."


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
