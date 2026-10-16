"""Runtime configuration for the Aniimo UID top-up service.

All values are overridable via environment variables (prefix ``ANIIMO_``) or a
local ``.env`` file. Defaults target the Aniimo *global* region (TWD / Taiwan)
with MyCard as the only payment channel, matching the assignment brief.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANIIMO_", env_file=".env", extra="ignore"
    )

    # --- Upstream Aniimo (FunPlus / Kingsgroup) hosts -----------------------
    store_api_base: str = "https://worldx-store-coin.kingsgroupgames.com"
    account_api_base: str = "https://worldx-office-api.aniimo.com"

    # Common request envelope. These are NOT secrets — they are baked into the
    # public web bundle (game_id 999002 = worldx_global).
    game_id: int = 999002
    game_project: str = "worldx_global"
    source: str = "web"

    # --- Region / currency / payment lock -----------------------------------
    # The web store derives currency from the caller IP. To obtain TWD + MyCard
    # the upstream calls must egress from a Taiwan IP. Configure an outbound
    # proxy here (e.g. a TW residential/datacenter proxy) when running for real.
    region: Literal["TW"] = "TW"
    expected_currency: str = "TWD"
    payment_channel_id: str = "mycard"
    payment_sub_channel_id: str = "mycard"
    outbound_proxy: str | None = None  # e.g. "http://user:pass@tw-proxy:8080"
    # Pretend-geo header some CDNs honor; real geolocation still needs a TW IP.
    accept_language: str = "zh-TW,zh;q=0.9"

    # --- Role / UID verification provider -----------------------------------
    # Which RoleProvider backs POST /api/v1/aniimo/verify.
    #   "partner" - call a publisher partner UID-lookup endpoint (prod).
    #   "session" - bridge through an authenticated store-account session.
    #   "mock"    - deterministic fixtures, for local dev / CI / demo.
    role_provider: Literal["partner", "session", "mock"] = "mock"

    # Partner provider (only used when role_provider == "partner").
    partner_verify_url: str | None = None
    partner_api_key: str | None = None

    # Session provider (only used when role_provider == "session").
    # The office API uses an operator-owned Bearer credential after login.
    # It reads only that account's roles; it is not an arbitrary UID lookup.
    # Bearer credential from an operator-owned test session; never a public
    # request parameter and never checked into source control.
    account_bearer_token: SecretStr | None = None
    # Deprecated compatibility setting. Only sent when explicitly configured.
    account_session_cookie: str | None = None

    # --- Resilience knobs ----------------------------------------------------
    http_timeout: float = 15.0
    http_retries: int = 2
    http_retry_backoff: float = 0.5
    # Retry policy is split: reads may retry, order placement may NOT auto-retry
    # on an ambiguous failure (see AniimoClient). order_connect_retries only
    # covers failures that provably never reached the server.
    order_connect_retries: int = 0
    # Per-client rate limit on our own endpoints (token bucket).
    rate_limit_per_min: int = 30
    rate_limit_burst: int = 10
    # Trust X-Forwarded-For only when the direct peer is a known proxy, so a
    # client cannot spoof the header to dodge the rate limiter.
    trusted_proxies: list[str] = Field(default_factory=list)  # e.g. ["127.0.0.1"]
    # Upstream circuit breaker: open after N consecutive upstream failures.
    # Applied PER STAGE — a separate breaker guards reads vs order placement, so
    # repeated order failures open the circuit even when reads keep succeeding.
    breaker_threshold: int = 5
    breaker_cooldown: float = 30.0

    # Durable idempotency/dedupe state for order placement.
    #   "memory" - in-process (dev/tests; lost on restart)
    #   "sqlite" - survives restarts (single node); use Redis for a cluster.
    idempotency_backend: Literal["memory", "sqlite"] = "memory"
    idempotency_db_path: str = "order_state.db"
    # Completed entries age out after this; 'unknown'/'in_flight' never expire.
    idempotency_done_ttl: float = 86400.0

    service_name: str = "aniimo-uid-topup"
    env: Literal["dev", "staging", "prod"] = Field(default="dev")

    @property
    def is_mock_mode(self) -> bool:
        """System-wide mock mode: character lookup, catalogue and ordering are
        all simulated. Driven by the role provider so the two never diverge."""
        return self.role_provider == "mock"

    @model_validator(mode="after")
    def _guard_prod(self) -> "Settings":
        # Never let a production deployment silently run on mock data.
        if self.env == "prod" and self.role_provider == "mock":
            raise ValueError(
                "ANIIMO_ROLE_PROVIDER=mock is not allowed with ANIIMO_ENV=prod; "
                "configure a real provider (partner|session)."
            )
        # Order-dedupe state must be durable in production — memory is lost on
        # restart and is not shared across workers.
        if self.env == "prod" and self.idempotency_backend == "memory":
            raise ValueError(
                "ANIIMO_IDEMPOTENCY_BACKEND=memory is not allowed with ANIIMO_ENV=prod; "
                "use a durable backend (sqlite, or Redis in a cluster)."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
