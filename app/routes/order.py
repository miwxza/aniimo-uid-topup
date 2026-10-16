"""Packages + UID-based order creation."""
from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from fastapi import APIRouter, Depends, Request

from app.aniimo.idempotency import OrderGuard
from app.aniimo.providers import RoleProvider
from app.aniimo.store import Store, idempotency_key, order_fingerprint
from app.config import Settings
from app.deps import (
    client_key,
    get_guard,
    get_limiter,
    get_order_breaker,
    get_provider,
    get_read_breaker,
    get_settings,
    get_store,
)
from app.errors import (
    CaptchaRequiredError,
    IpBlockedError,
    OrderResultUnknownError,
    ProductNotFoundError,
    RateLimitedError,
    UpstreamUnavailableError,
)
from app.models import CreateOrderRequest, OrderResponse, Package, PackagesResponse
from app.ratelimit import CircuitBreaker, TokenBucketLimiter
from app.validation import normalize_server, normalize_uid

router = APIRouter(prefix="/api/v1/aniimo", tags=["order"])

T = TypeVar("T")

# Only genuine upstream-health failures should open a circuit breaker; validation
# / business rejects (bad UID, wrong currency, product not found) must not.
_UPSTREAM_FAULTS = (
    UpstreamUnavailableError,
    CaptchaRequiredError,
    IpBlockedError,
    OrderResultUnknownError,
)


def _admit(breaker: CircuitBreaker) -> None:
    breaker.before()  # raises UpstreamUnavailableError when the circuit is open


async def _run(breaker: CircuitBreaker, make: Callable[[], Awaitable[T]]) -> T:
    """Execute an already-admitted upstream call, recording breaker outcome."""
    try:
        result = await make()
    except _UPSTREAM_FAULTS:
        breaker.record_failure()
        raise
    except RateLimitedError as e:
        if e.code == "RATE_LIMITED_UPSTREAM":
            breaker.record_failure()
        raise
    except Exception:
        raise  # business/app error — upstream is healthy
    breaker.record_success()
    return result


async def _upstream(breaker: CircuitBreaker, make: Callable[[], Awaitable[T]]) -> T:
    _admit(breaker)
    return await _run(breaker, make)


@router.get("/packages", response_model=PackagesResponse)
async def packages(
    request: Request,
    server: str = "1",
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    read_breaker: CircuitBreaker = Depends(get_read_breaker),
) -> PackagesResponse:
    limiter.check(f"packages:{client_key(request)}")
    server = normalize_server(server)
    pkgs = await _upstream(read_breaker, lambda: store.list_packages(server))
    currency = pkgs[0].currency if pkgs else settings.expected_currency
    return PackagesResponse(
        currency=currency, payment_channel=settings.payment_channel_id, packages=pkgs
    )


@router.post("/order", response_model=OrderResponse)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    provider: RoleProvider = Depends(get_provider),
    store: Store = Depends(get_store),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    read_breaker: CircuitBreaker = Depends(get_read_breaker),
    order_breaker: CircuitBreaker = Depends(get_order_breaker),
    guard: OrderGuard = Depends(get_guard),
) -> OrderResponse:
    limiter.check(f"order:{client_key(request)}")
    uid = normalize_uid(body.uid)
    server = normalize_server(body.server)
    fp = order_fingerprint(uid, server, body.package_id, body.quantity)
    key = idempotency_key(uid, server, body.package_id, body.quantity, body.idempotency_key)

    # Dedupe FIRST, before any upstream work, so a reused key with different data
    # is rejected and an exact replay returns the cached result.
    cached = guard.begin(key, fp)

    # Resolve the character (read). Failure here is pre-dispatch → free the key.
    try:
        character = await _upstream(read_breaker, lambda: provider.resolve(uid, server))
        if cached is not None:
            return _to_response(cached, character)
        pkgs = await _upstream(read_breaker, lambda: store.list_packages(server, character.openid))
        package: Package | None = next((p for p in pkgs if p.package_id == body.package_id), None)
        if package is None:
            raise ProductNotFoundError(body.package_id)
        await _upstream(read_breaker, lambda: store.prepare_order(character, package))
    except Exception:
        guard.abort(key)  # nothing was dispatched — safe to free the key
        raise

    # Dispatch phase. From here the outcome is uncertain on ANY failure, so keep
    # the key as 'unknown' (blocks resends until the status is checked).
    try:
        _admit(order_breaker)  # circuit open → pre-dispatch, free the key
    except Exception:
        guard.abort(key)
        raise
    try:
        result = await _run(
            order_breaker, lambda: store.dispatch_order(character, package, body.quantity, key)
        )
    except Exception:
        guard.mark_unknown(key, fp)
        raise

    guard.complete(key, fp, result)
    return _to_response(result, character)


def _to_response(result, character) -> OrderResponse:
    return OrderResponse(
        order_id=result.order_id,
        status=result.status,
        currency=result.currency,
        amount=result.amount,
        payment_channel="mycard",
        pay_url=result.pay_url,
        character=character,
        is_mock=result.is_mock,
    )
