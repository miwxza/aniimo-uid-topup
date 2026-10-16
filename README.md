# Aniimo UID Top-Up API

A RESTful service that reverse-engineers the **Aniimo** webshop
(`https://www.aniimo.com/store/`) to enable **UID-based top-ups** — verify a
character from its UID and create a top-up order **without the customer's
password** — locked to **Taiwan / TWD / MyCard**.

This is the "Via Login → UID Top-Up" conversion described in the technical
assessment.

> **TL;DR of the reverse engineering:** store calls carry **no signature or
> encryption**, only a fixed envelope `{game_id, game_project, source,
> server_id}`; orders are bound to a role **`openid`** and the store trusts it,
> so `place_order` needs no customer credentials. The one thing the public shop
> does **not** expose is a "look up any UID → character" endpoint — so that
> single step is a pluggable provider. Full write-up:
> [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md).

## Architecture

```
client ──► FastAPI ──► RoleProvider ─┐  (uid+server → character + openid)
                        │            │     partner API | account session | mock
                        └─► Store ────────► Aniimo store API  (LiveStore)
                                     └────► simulated          (MockStore)
                                            game_products / place_order (openid-bound)
```

**Mock mode vs live mode** are system-wide and driven by the provider, so they
never diverge:

- `ANIIMO_ROLE_PROVIDER=mock` → **mock mode**: character lookup, catalogue and
  ordering are all simulated (`MockStore`). Nothing hits the real API. Mock
  characters are flagged `is_mock: true` and carry a `note`.
- `partner` / `session` → **live mode** (`LiveStore`): real upstream, and it
  **refuses mock character data**. `ANIIMO_ENV=prod` with `mock` fails at
  startup.

- **RoleProvider** (`app/aniimo/providers.py`) — resolves `(uid, server)` →
  character. The shop has no public UID lookup, so this is a strategy:
  - `partner` — a publisher partner UID-lookup endpoint (production). **Not yet
    connected** — needs a real endpoint + key.
  - `session` — bridge through one authenticated store-account session;
    **resolves only that account's own roles**, not arbitrary customer UIDs.
  - `mock` — deterministic, clearly-flagged fixtures for dev / CI / demo. The
    fixture openid is a constant, **never derived from the UID**.
- **Store** (`app/aniimo/store.py`) — `LiveStore` reads the real catalogue and
  places the openid-bound order; before ordering it **enforces TWD + MyCard from
  upstream** (`REGION_CURRENCY_MISMATCH` / `PAYMENT_CHANNEL_UNAVAILABLE`),
  dedupes with an idempotency guard, and **validates the order response** before
  reporting success (`UPSTREAM_INVALID_RESPONSE`).
- **Resilience** (`app/ratelimit.py`, `app/errors.py`) — per-client token
  bucket (X-Forwarded-For trusted only from configured proxies), a circuit
  breaker that counts only upstream faults, split read-vs-order retry (an order
  read-timeout is reported `ORDER_RESULT_UNKNOWN`, never auto-resent), and a
  stable error contract. See [`docs/EDGE_CASES.md`](docs/EDGE_CASES.md).

### What is actually tested vs pending

| Area | Status |
|---|---|
| RE of store/account API, envelope, openid ordering, no-signature | Verified by reading the live bundles + live unauthenticated `store/init`/`game_products` calls |
| `verify` / `packages` / `order` in **mock mode** | Fully tested (`pytest`) |
| **live-mode** order flow, TWD/MyCard enforcement, timeout→unknown, duplicate guard, invalid-response, breaker | Tested with the upstream **mocked** (`respx`) |
| `partner` provider against a **real** endpoint | **Pending** — no partner endpoint available |
| Real **TWD + MyCard** pricing from upstream | **Pending** — needs a Taiwan egress (`ANIIMO_OUTBOUND_PROXY`) |

> "Verified live" in this repo means the **unauthenticated catalogue** calls
> (`store/init`, `game_products`) were run against the real host and returned the
> documented shape. The order placement path is proven against a mocked upstream,
> not fired against production.

## API

Base: `/api/v1/aniimo`. Interactive docs at `/docs` once running.

### `POST /verify`
```json
{ "uid": "412000126173", "server": "4" }
```
→
```json
{
  "verified": true,
  "character": { "uid": "412000126173", "server": "4", "role_name": "Sweettoast", "level": 80, "openid": "MOCK-openid-sweettoast", "is_mock": true },
  "source": "mock",
  "note": "Illustrative mock result — not a live game lookup. ..."
}
```

### `GET /packages?server=4`
Returns the live catalogue (currency follows the egress IP — TWD from a Taiwan
egress, otherwise e.g. THB) and the pinned MyCard channel.

### `POST /order`
```json
{ "uid": "412000126173", "server": "4", "package_id": "290002", "quantity": 1 }
```
→ order id, amount, MyCard `pay_url`, and the verified character. The flow is:
verify UID → resolve package from the live catalogue → `place_order` via openid.

Error contract and all codes: [`docs/EDGE_CASES.md`](docs/EDGE_CASES.md).

## Run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   *nix: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # defaults to the mock provider
uvicorn app.main:app --reload
```

Open http://localhost:8000/docs, or fire the sample requests in
[`requests.http`](requests.http) / import
[`docs/postman_collection.json`](docs/postman_collection.json).

### Switching provider

Set in `.env`:

```
ANIIMO_ROLE_PROVIDER=partner
ANIIMO_PARTNER_VERIFY_URL=https://partner.example.com/aniimo/role/lookup
ANIIMO_PARTNER_API_KEY=...
```

### Getting TWD + MyCard

Upstream currency is IP-derived. Point outbound traffic at a Taiwan egress:

```
ANIIMO_OUTBOUND_PROXY=http://user:pass@tw-proxy.example.com:8080
```

## Test

```bash
pytest -q
```

Tests cover: mock-mode verify/packages/order (incl. `is_mock` flagging and
product-not-found), and live-mode (upstream mocked with `respx`) order success,
`REGION_CURRENCY_MISMATCH`, `PAYMENT_CHANNEL_UNAVAILABLE`, order read-timeout →
`ORDER_RESULT_UNKNOWN` (asserting it is **not** re-sent), duplicate-order replay,
`UPSTREAM_INVALID_RESPONSE`, `IP_BLOCKED`, circuit-breaker opening, `LiveStore`
refusing mock character data, and `prod`+`mock` failing at startup.

## Test account (from the brief)

A dev account is provided for the `session` provider. Login requires an **email
OTP each time**, so capture the store-account session cookie after a manual
login and set `ANIIMO_ACCOUNT_SESSION_COOKIE`. Keep real credentials out of git.

## Layout

```
app/
  main.py            FastAPI app, error handler, lifespan wiring
  config.py          settings (env ANIIMO_*)
  models.py          request/response schemas
  errors.py          typed errors → JSON contract
  ratelimit.py       token bucket + circuit breaker
  deps.py            DI providers
  aniimo/
    client.py        low-level upstream HTTP client (envelope, retries, unwrap)
    providers.py     RoleProvider strategies (partner | session | mock)
    store.py         catalogue + openid-bound order
  routes/
    verify.py        POST /verify
    order.py         GET /packages, POST /order
docs/                reverse-engineering, edge cases, Postman collection
tests/               pytest + respx
requests.http        REST Client / JetBrains HTTP samples
```

## Scope & honesty

- **Reverse engineering** of the store/account API (envelope, openid-bound
  ordering, no signature) is real and documented, confirmed against the live
  **unauthenticated** endpoints.
- **Arbitrary-UID verification** is the one thing the public shop does not
  expose. It is a pluggable provider; `partner`/`session` are the real
  integration points and are **not yet connected to a live source**. `mock`
  ships so the whole flow runs end-to-end and is clearly flagged as simulated.
- **Order placement** is implemented and tested against a **mocked** upstream;
  it is not fired against production, and real TWD/MyCard pricing needs a Taiwan
  egress. See the "actually tested vs pending" table above.

A security-research / tooling exercise against the operator's own shop.
