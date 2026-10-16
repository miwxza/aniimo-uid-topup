"""Runtime configuration for the Aniimo UID top-up service.

All values are overridable via environment variables (prefix ``ANIIMO_``) or a
local ``.env`` file. Defaults target the Aniimo *global* region (TWD / Taiwan)
with MyCard as the only payment channel, matching the assignment brief.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANIIMO_", env_file=".env", extra="ignore"
    )

    # --- Upstream Aniimo (FunPlus / Kingsgroup) hosts -----------------------
    store_api_base: str = "https://worldx-store-coin.kingsgroupgames.com"
    account_api_base: str = "https://worldx-store-account.kingsgroupgames.com"

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
    # A store-account session cookie captured after an OTP login with the dev
    # account. Lets the service read that account's own roles via /api/player/roles.
    account_session_cookie: str | None = None

    # --- Resilience knobs ----------------------------------------------------
    http_timeout: float = 15.0
    http_retries: int = 2
    http_retry_backoff: float = 0.5
    # Per-client rate limit on our own endpoints (token bucket).
    rate_limit_per_min: int = 30
    rate_limit_burst: int = 10
    # Upstream circuit breaker: open after N consecutive upstream failures.
    breaker_threshold: int = 5
    breaker_cooldown: float = 30.0

    service_name: str = "aniimo-uid-topup"
    env: Literal["dev", "staging", "prod"] = Field(default="dev")


@lru_cache
def get_settings() -> Settings:
    return Settings()
