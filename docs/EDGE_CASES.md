# Edge Cases & Resilience

How the service handles the failure modes the brief calls out. All failures map
to a stable JSON contract:

```json
{ "error": { "code": "STRING_CODE", "message": "...", "retry_after": 60, "details": {} } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `UID_NOT_FOUND` | 404 | No character for UID/server |
| `SERVER_INVALID` | 422 | Unknown/unsupported server (format or value) |
| `PRODUCT_NOT_FOUND` | 404 | package_id not in the live catalogue (distinct from bad server) |
| `REGION_CURRENCY_MISMATCH` | 409 | Catalogue not priced in TWD (needs a Taiwan egress) |
| `PAYMENT_CHANNEL_UNAVAILABLE` | 409 | Upstream does not offer MyCard for this context |
| `DUPLICATE_ORDER` | 409 | Identical order already in flight (idempotency guard) |
| `IDEMPOTENCY_KEY_CONFLICT` | 409 | Key reused with different order data |
| `ORDER_RESULT_UNKNOWN` | 202 | place_order outcome unconfirmed (timeout / 5xx / bad response) — check status, do not resend |
| `UPSTREAM_INVALID_RESPONSE` | 502 | Order "ok" but missing order_id / amount / currency per contract |
| `MOCK_DATA_IN_LIVE_MODE` | 409 | Live order attempted from mock character data |
| `RATE_LIMITED` | 429 | Our own per-client token bucket tripped |
| `RATE_LIMITED_UPSTREAM` | 429 | Upstream returned 429 |
| `CAPTCHA_REQUIRED` | 423 | Upstream served a CAPTCHA/interstitial instead of JSON |
| `IP_BLOCKED` | 403 | Upstream WAF/geo block |
| `RISK_CONTROL_BLOCK` | 409 | Upstream risk code (e.g. 106042 order-too-frequent) |
| `UPSTREAM_UNAVAILABLE` | 502 | Network error / 5xx / circuit open |
| `UPSTREAM_BUSINESS_ERROR` | 502 | Any other non-zero business code |

## Duplicate orders (idempotency)

The order flow is split into a **pre-dispatch** phase (character lookup,
catalogue, region/channel enforcement — nothing sent yet) and a **dispatch**
phase (`place_order` + response validation). The idempotency guard treats them
differently:

- **Key ↔ data binding.** The key (client-supplied or derived from
  `uid|server|package_id|quantity`) is stored with a fingerprint of those
  fields. Reusing a key with *different* data → `IDEMPOTENCY_KEY_CONFLICT`
  (409); it is never replayed onto a different character/amount. An exact replay
  of a completed order returns the cached result (one order, not two).
- **Pre-dispatch failure** (validation, wrong currency, product not found,
  circuit open before send) → the key is **freed**; the order was never sent, so
  a corrected retry is allowed.
- **Post-dispatch failure** (read timeout, 5xx, or a response that fails
  validation) → the outcome is unknown, so the key is kept in an **`unknown`**
  state and the call returns `ORDER_RESULT_UNKNOWN` (202). Any identical resend
  is blocked (also `ORDER_RESULT_UNKNOWN`) until the order status is checked and
  the entry is resolved. This is what prevents a double charge after an
  ambiguous `place_order`.
- **Replay is cheap and safe.** A completed order is cached with its character,
  so an exact replay returns immediately **before any upstream call** — a
  provider/catalogue outage during a resend cannot disturb the `done` entry.
  `abort` only ever removes the `in_flight` slot it created, never a `done` or
  `unknown` entry.
- **Atomic reservation.** Admission uses an atomic `INSERT … ON CONFLICT DO
  NOTHING`, so two workers racing on the same key cannot both be admitted —
  exactly one wins, the other is rejected.
- **Durability.** State uses a pluggable backend: `sqlite` survives process
  restarts (`ANIIMO_IDEMPOTENCY_BACKEND=sqlite`), `memory` is for dev/tests. Use
  Redis for a cluster — same contract. **Production refuses `memory`** (startup
  fails) so order state is never lost on restart. Only completed (`done`)
  entries age out (`idempotency_done_ttl`); `unknown` / `in_flight` entries are
  **never** TTL-expired, so an unconfirmed order cannot silently become
  resendable.

## Circuit breaker (per stage)

Reads and order placement have **separate** breakers, so a run of successful
reads can no longer reset the failure count that order placement is
accumulating. Repeated `place_order` faults open the *order* breaker
independently and short-circuit further placements (pre-dispatch, so the key is
freed), while reads stay available.

## Region / currency / channel enforcement

Before an order is placed, `LiveStore` checks **from upstream** that the package
currency is `TWD` and that MyCard is in the available channels (from
`store/init`). A `"mycard"` value in config alone is not treated as proof.

## Order response validation

A `place_order` success is only reported when the response carries a valid
`order_id` and (when present) a matching `amount`/`currency`; otherwise
`UPSTREAM_INVALID_RESPONSE` instead of a false `created`.

## Rate limiting

- **Inbound:** per-client token bucket (`app/ratelimit.py`), keyed on
  `X-Forwarded-For` → peer IP, separate buckets per operation
  (`verify:` / `packages:` / `order:`). Defaults: 30/min, burst 10. Returns
  `429` + `Retry-After`. Swap for a Redis bucket when running multi-node.
- **Outbound:** retries with exponential backoff on connect/timeout
  (`http_retries`, `http_retry_backoff`). On upstream `429` we surface
  `RATE_LIMITED_UPSTREAM` with the upstream `Retry-After` rather than hammering.

## IP blocking / geo

- Upstream currency is IP-derived and TW pricing needs a Taiwan egress. Route
  outbound calls through `ANIIMO_OUTBOUND_PROXY` (TW proxy pool). Rotate proxies
  on repeated `IP_BLOCKED` to avoid a single IP getting flagged.
- A `403` from upstream is treated as `IP_BLOCKED` so the caller/ops can react
  (rotate egress) instead of retrying blindly.

## CAPTCHA / risk challenge

- The verify/order endpoints are machine-to-machine. If upstream responds with
  HTML or a CAPTCHA interstitial (detected by non-JSON content-type +
  `captcha`/`verify`/`<html`), we fail fast with `CAPTCHA_REQUIRED` (423)
  rather than trying to solve it. Mitigations: keep request volume under the
  risk threshold, warm sessions, reuse the ShuMei device token from a real
  browser session, and fall back to a partner API that is not CAPTCHA-gated.
- A **circuit breaker** (`app/ratelimit.py::CircuitBreaker`) opens after N
  consecutive upstream failures and cools down, so a CAPTCHA wall or outage
  doesn't amplify into a request storm.

## Invalid UID / server

- Format validation at the edge (pydantic): UID must be 4–20 digits.
- Semantic validation by the `RoleProvider`: unknown UID → `UID_NOT_FOUND`.
- Unknown `package_id` for the server → `SERVER_INVALID`.

## Order safety

- Ordering has its own rate-limit key and runs behind the circuit breaker.
- Risk codes (e.g. `106042`) are surfaced as `RISK_CONTROL_BLOCK` (409), not
  retried.
- The order is openid-bound and never carries a customer password.
