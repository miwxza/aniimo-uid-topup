"""Packages + UID-based order creation."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.aniimo.providers import RoleProvider
from app.aniimo.store import StoreService
from app.config import Settings
from app.deps import (
    client_key,
    get_breaker,
    get_limiter,
    get_provider,
    get_settings,
    get_store,
)
from app.errors import InvalidServerError
from app.models import (
    CreateOrderRequest,
    OrderResponse,
    Package,
    PackagesResponse,
)
from app.ratelimit import CircuitBreaker, TokenBucketLimiter

router = APIRouter(prefix="/api/v1/aniimo", tags=["order"])


@router.get("/packages", response_model=PackagesResponse)
async def packages(
    request: Request,
    server: str = "1",
    store: StoreService = Depends(get_store),
    settings: Settings = Depends(get_settings),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    breaker: CircuitBreaker = Depends(get_breaker),
) -> PackagesResponse:
    limiter.check(f"packages:{client_key(request)}")
    breaker.before()
    try:
        pkgs = await store.list_packages(server)
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    currency = pkgs[0].currency if pkgs else settings.expected_currency
    return PackagesResponse(
        currency=currency, payment_channel=settings.payment_channel_id, packages=pkgs
    )


@router.post("/order", response_model=OrderResponse)
async def create_order(
    body: CreateOrderRequest,
    request: Request,
    provider: RoleProvider = Depends(get_provider),
    store: StoreService = Depends(get_store),
    settings: Settings = Depends(get_settings),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    breaker: CircuitBreaker = Depends(get_breaker),
) -> OrderResponse:
    # Ordering is the most abuse-sensitive op → stricter key.
    limiter.check(f"order:{client_key(request)}")

    # 1. Verify the character (also yields the openid needed to bind the order).
    character = await provider.resolve(body.uid, body.server)

    # 2. Resolve the selected package from the live catalogue.
    pkgs = await store.list_packages(body.server, character.openid)
    package: Package | None = next(
        (p for p in pkgs if p.package_id == body.package_id), None
    )
    if package is None:
        raise InvalidServerError(body.server)

    # 3. Place the order via openid (no customer password involved).
    breaker.before()
    try:
        result = await store.create_order(character, package, body.quantity)
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()

    return OrderResponse(
        order_id=str(result.get("order_id", "")),
        status=str(result.get("status", "created")),
        currency=package.currency or settings.expected_currency,
        amount=package.final_price * body.quantity,
        payment_channel=settings.payment_channel_id,
        pay_url=result.get("pay_url") or result.get("payUrl"),
        character=character,
    )
