"""Shared singletons wired at app startup and exposed via FastAPI dependencies."""
from __future__ import annotations

from fastapi import Request

from app.aniimo.idempotency import OrderGuard
from app.aniimo.providers import RoleProvider
from app.aniimo.store import Store
from app.config import Settings
from app.ratelimit import CircuitBreaker, TokenBucketLimiter


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_provider(request: Request) -> RoleProvider:
    return request.app.state.provider


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_limiter(request: Request) -> TokenBucketLimiter:
    return request.app.state.limiter


def get_read_breaker(request: Request) -> CircuitBreaker:
    return request.app.state.read_breaker


def get_order_breaker(request: Request) -> CircuitBreaker:
    return request.app.state.order_breaker


def get_guard(request: Request) -> OrderGuard:
    return request.app.state.guard


def client_key(request: Request) -> str:
    """Rate-limit key: the client IP.

    ``X-Forwarded-For`` is honoured **only** when the direct peer is a trusted
    proxy, otherwise a client could spoof the header and dodge the limiter.
    """
    settings: Settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    if peer in settings.trusted_proxies:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer
