"""Packages + UID-based order creation."""
from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

from fastapi import APIRouter, Depends, Request

from app.aniimo.providers import RoleProvider
from app.aniimo.store import Store, OrderGuard, idempotency_key
from app.config import Settings
from app.deps import (
    client_key,
    get_breaker,
    get_guard,
    get_limiter,
    get_provider,
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

# Only genuine upstream-health failures should open the circuit breaker;
# validation / business rejects (bad UID, wrong currency, product not found)
# must not.
_UPSTREAM_FAULTS = (
    UpstreamUnavailableError,
    CaptchaRequiredError,
    IpBlockedError,
    OrderResultUnknownError,
)


async def _upstream(breaker: CircuitBreaker, make: Callable[[], Awaitable[T]]) -> T:
    """Run an upstream call behind the breaker, counting only upstream faults.

    Takes a factory so the coroutine is created only after the breaker admits
    the call (no un-awaited coroutine when the circuit is open).
    """
    breaker.before()  # may raise UpstreamUnavailableError if circuit is open
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
        # App/business error — upstream is healthy, don't penalise it.
        raise
    breaker.record_success()
    return result


@router.get("/packages", response_model=PackagesResponse)
async def packages(
    request: Request,
    server: str = "1",
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    breaker: CircuitBreaker = Depends(get_breaker),
) -> PackagesResponse:
    limiter.check(f"packages:{client_key(request)}")
    server = normalize_server(server)
    pkgs = await _upstream(breaker, lambda: store.list_packages(server))
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
    settings: Settings = Depends(get_settings),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    breaker: CircuitBreaker = Depends(get_breaker),
    guard: OrderGuard = Depends(get_guard),
) -> OrderResponse:
    limiter.check(f"order:{client_key(request)}")
    uid = normalize_uid(body.uid)
    server = normalize_server(body.server)

    # 1. Verify the character (yields the openid needed to bind the order).
    character = await _upstream(breaker, lambda: provider.resolve(uid, server))

    # 2. Dedupe guard — identical in-flight request is rejected; a completed one
    #    replays its cached result instead of ordering again.
    key = idempotency_key(uid, server, body.package_id, body.quantity, body.idempotency_key)
    cached = guard.begin(key)
    if cached is not None:
        return _to_response(cached, character)

    try:
        # 3. Resolve the package from the live catalogue.
        pkgs = await _upstream(breaker, lambda: store.list_packages(server, character.openid))
        package: Package | None = next(
            (p for p in pkgs if p.package_id == body.package_id), None
        )
        if package is None:
            raise ProductNotFoundError(body.package_id)

        # 4. Place the order (region/channel enforced inside the store; response
        #    validated before success).
        result = await _upstream(
            breaker, lambda: store.create_order(character, package, body.quantity, key)
        )
    except OrderResultUnknownError:
        # Keep the key in flight so an immediate resend is blocked; caller must
        # check order status before retrying.
        raise
    except Exception:
        guard.abort(key)
        raise

    guard.complete(key, result)
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
