"""Low-level HTTP client for the Aniimo (FunPlus/Kingsgroup) web-store APIs.

Reverse-engineered contract (see docs/REVERSE_ENGINEERING.md):

* All business calls are plain JSON POSTs. **No signature / HMAC / encryption**
  is applied to the payload — the only "auth" for the store API is the common
  envelope ``{game_id, game_project, source, server_id}`` plus, for ordering,
  an ``openid`` that identifies the target role.
* Responses use a doubly-wrapped envelope:
  HTTP 200 → ``{"code": 0, "data": {...}}`` where an inner ``code != 0`` is a
  business error. We normalise both layers here.
* Currency is chosen by the **caller IP**. For TWD/MyCard the client must
  egress from a Taiwan IP; configure ``outbound_proxy``.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings
from app.errors import (
    CaptchaRequiredError,
    IpBlockedError,
    RateLimitedError,
    RiskControlError,
    UpstreamBusinessError,
    UpstreamUnavailableError,
)

# Upstream business codes we recognise (partial; extend as observed).
UPSTREAM_ORDER_TOO_FREQUENT = 106042
UPSTREAM_RISK_CODES = {106042}


class AniimoClient:
    """Thin async wrapper around the two upstream hosts."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.s = settings
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "AniimoClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.s.http_timeout,
                proxy=self.s.outbound_proxy,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Accept-Language": self.s.accept_language,
                    # A real browser UA reduces trivial bot fingerprinting.
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Origin": "https://www.aniimo.com",
                    "Referer": "https://www.aniimo.com/store/",
                },
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # -- envelope helpers ---------------------------------------------------
    def _envelope(self, server_id: str | int, extra: dict | None = None) -> dict:
        body = {
            "game_id": self.s.game_id,
            "game_project": self.s.game_project,
            "source": self.s.source,
            "server_id": server_id,
        }
        if extra:
            body.update(extra)
        return body

    # -- transport ----------------------------------------------------------
    async def _post(self, base: str, path: str, payload: dict) -> dict[str, Any]:
        assert self._client is not None
        url = base.rstrip("/") + path
        last_exc: Exception | None = None
        for attempt in range(self.s.http_retries + 1):
            try:
                resp = await self._client.post(url, json=payload)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                await asyncio.sleep(self.s.http_retry_backoff * (2**attempt))
                continue
            return self._unwrap(resp)
        raise UpstreamUnavailableError(str(last_exc) if last_exc else "no response")

    def _unwrap(self, resp: httpx.Response) -> dict[str, Any]:
        # --- HTTP-layer anti-abuse signals ---------------------------------
        if resp.status_code == 429:
            raise RateLimitedError(
                retry_after=int(resp.headers.get("Retry-After", 60)), upstream=True
            )
        if resp.status_code == 403:
            # Akamai / WAF geo or risk block.
            raise IpBlockedError()
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            # A CAPTCHA/interstitial HTML page instead of JSON.
            text = resp.text[:200].lower()
            if "captcha" in text or "verify" in text or "<html" in text:
                raise CaptchaRequiredError()
            raise UpstreamUnavailableError(f"non-JSON upstream response ({resp.status_code})")
        if resp.status_code >= 500:
            raise UpstreamUnavailableError(f"upstream {resp.status_code}")

        outer = resp.json()
        # Outer transport code.
        if outer.get("code") not in (0, None):
            raise UpstreamBusinessError(outer.get("code"), outer.get("message") or outer.get("msg"))
        data = outer.get("data", outer)
        # Inner business code (store APIs nest another code).
        if isinstance(data, dict) and data.get("code") not in (0, None):
            icode = data.get("code")
            msg = data.get("message") or data.get("msg")
            if icode in UPSTREAM_RISK_CODES:
                raise RiskControlError(icode, msg)
            raise UpstreamBusinessError(icode, msg)
        return data if isinstance(data, dict) else {"data": data}

    # -- store endpoints ----------------------------------------------------
    async def store_init(self, server_id: str | int, openid: str | None = None) -> dict:
        extra = {"openid": openid} if openid else {}
        return await self._post(
            self.s.store_api_base, "/api/store/init", self._envelope(server_id, extra)
        )

    async def game_products(self, server_id: str | int, openid: str | None = None) -> dict:
        extra = {"openid": openid} if openid else {}
        return await self._post(
            self.s.store_api_base, "/api/game_products", self._envelope(server_id, extra)
        )

    async def place_order(self, server_id: str | int, order: dict) -> dict:
        return await self._post(
            self.s.store_api_base, "/api/place_order", self._envelope(server_id, order)
        )

    async def preload_order(self, server_id: str | int, order: dict) -> dict:
        return await self._post(
            self.s.store_api_base, "/api/preload_order", self._envelope(server_id, order)
        )

    # -- account endpoints (session-bridged) --------------------------------
    async def player_roles(self, session_cookie: str) -> dict:
        assert self._client is not None
        url = self.s.account_api_base.rstrip("/") + "/api/player/roles"
        resp = await self._client.get(url, headers={"Cookie": session_cookie})
        return self._unwrap(resp)
