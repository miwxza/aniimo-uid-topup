"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.aniimo.providers import build_provider
from app.aniimo.store import StoreService
from app.config import get_settings
from app.errors import AniimoError
from app.ratelimit import CircuitBreaker, TokenBucketLimiter
from app.routes import order, verify

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("aniimo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.provider = build_provider(settings)
    app.state.store = StoreService(settings)
    app.state.limiter = TokenBucketLimiter(settings.rate_limit_per_min, settings.rate_limit_burst)
    app.state.breaker = CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown)
    log.info("started: provider=%s region=%s", settings.role_provider, settings.region)
    yield


app = FastAPI(
    title="Aniimo UID Top-Up API",
    version="1.0.0",
    description=(
        "RESTful service that reverse-engineers the Aniimo webshop to enable "
        "UID-based top-ups (Taiwan / TWD / MyCard) without the customer's password."
    ),
    lifespan=lifespan,
)

app.include_router(verify.router)
app.include_router(order.router)


@app.exception_handler(AniimoError)
async def aniimo_error_handler(request: Request, exc: AniimoError) -> JSONResponse:
    headers = {}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict(), headers=headers)


@app.get("/health", tags=["meta"])
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "service": s.service_name, "provider": s.role_provider}
