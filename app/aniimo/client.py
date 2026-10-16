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
    AccountAuthenticationError,
    CaptchaRequiredError,
    IpBlockedError,
    OrderResultUnknownError,
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
            "server_id": int(server_id),
        }
        if extra:
            body.update(extra)
        return body

    # -- transport ----------------------------------------------------------
    async def _post(
        self,
        base: str,
        path: str,
        payload: dict,
        *,
        idempotent: bool = True,
        headers: dict | None = None,
    ) -> dict[str, Any]:
        """POST with a retry policy that depends on idempotency.

        For idempotent reads we retry connect/read failures. For a
        NON-idempotent call (``place_order``) we must never auto-resend once the
        request may have reached the server: a ``ReadTimeout`` means the outcome
        is unknown, so we raise ``OrderResultUnknownError`` instead of retrying,
        and only a pre-send ``ConnectError`` (provably never delivered) may be
        retried up to ``order_connect_retries``.
        """
        assert self._client is not None
        url = base.rstrip("/") + path
        max_attempts = (self.s.http_retries if idempotent else self.s.order_connect_retries) + 1
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                resp = await self._client.post(url, json=payload, headers=headers)
            except httpx.ReadTimeout as e:
                if not idempotent:
                    # Request may have been processed; do NOT resend.
                    raise OrderResultUnknownError() from e
                last_exc = e
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Never reached the server → safe to retry within limits.
                last_exc = e
            except httpx.RemoteProtocolError as e:
                if not idempotent:
                    raise OrderResultUnknownError() from e
                last_exc = e
            else:
                return self._unwrap(resp)
            if attempt < max_attempts - 1:
                await asyncio.sleep(self.s.http_retry_backoff * (2**attempt))
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

    async def place_order(
        self, server_id: str | int, order: dict, *, idempotency_key: str | None = None
    ) -> dict:
        # Non-idempotent: a read timeout must not trigger an automatic resend.
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data = await self._post(
            self.s.store_api_base,
            "/api/place_order",
            self._envelope(server_id, order),
            idempotent=False,
            headers=headers,
        )
        # Store transport removes the outer envelope. The frontend then reads
        # the inner business response's data for the actual order payload.
        inner = data.get("data")
        return inner if isinstance(inner, dict) else data

    async def preload_order(self, server_id: str | int, order: dict) -> dict:
        return await self._post(
            self.s.store_api_base, "/api/preload_order", self._envelope(server_id, order)
        )

    # -- account endpoints (session-bridged) --------------------------------
    async def player_roles(self, session_cookie: str | None = None) -> dict:
        """Office account API has a different envelope from the store API.

        Observed contract: GET, Authorization: Bearer, code=200, data.roles.
        Do not relax store code=0 handling to accommodate this endpoint.
        """
        assert self._client is not None
        headers = {}
        if self.s.account_bearer_token:
            token = self.s.account_bearer_token.get_secret_value().strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if session_cookie:
            headers["Cookie"] = session_cookie
        if not headers:
            raise AccountAuthenticationError()
        url = self.s.account_api_base.rstrip("/") + "/api/player/roles"
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError:
            raise UpstreamUnavailableError("Account service request failed.") from None
        if resp.status_code in (401, 403):
            raise AccountAuthenticationError()
        if resp.status_code == 429 or resp.status_code >= 500:
            return self._unwrap(resp)
        if resp.status_code != 200:
            raise UpstreamUnavailableError("Unexpected account service HTTP status.")
        try:
            body = resp.json()
        except ValueError:
            raise UpstreamUnavailableError("Account service returned invalid JSON.") from None
        if not isinstance(body, dict):
            raise UpstreamUnavailableError("Invalid account service envelope.")
        if body.get("code") in (401, 403):
            raise AccountAuthenticationError()
        if body.get("code") == 429:
            raise RateLimitedError(upstream=True)
        if body.get("code") != 200:
            raise UpstreamBusinessError(body.get("code"), "Account service rejected the lookup.")
        data = body.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("roles"), list):
            raise UpstreamUnavailableError("Invalid account roles response.")
        return data
