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
                        └─► StoreService ─► Aniimo store API
                                            game_products / place_order (openid-bound)
```

- **RoleProvider** (`app/aniimo/providers.py`) — resolves `(uid, server)` →
  character. Because the shop has no public UID lookup, this is a strategy:
  - `partner` — a publisher partner UID-lookup endpoint (production).
  - `session` — bridge through one authenticated store-account session
    (resolves only that account's own roles).
  - `mock` — deterministic fixtures for local dev / CI / demo (default).
- **StoreService** (`app/aniimo/store.py`) — reads the live catalogue and
  places the openid-bound order. This path is **real** and talks to the live
  upstream.
- **Resilience** (`app/ratelimit.py`, `app/errors.py`) — token-bucket rate
  limiting, upstream circuit breaker, and a stable error contract covering rate
  limits, IP blocking, CAPTCHA and invalid UID. See
  [`docs/EDGE_CASES.md`](docs/EDGE_CASES.md).

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
  "character": { "uid": "412000126173", "server": "4", "role_name": "Sweettoast", "level": 80, "openid": "oid_412000126173" },
  "source": "mock"
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

9 tests cover verify (ok / unknown / bad-format), the live-shaped packages and
order flow, and the risk/IP-block/CAPTCHA edge cases (upstream mocked with
`respx`).

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

The order path and catalogue are implemented against the **real** upstream and
verified live. Arbitrary-UID verification depends on a data source the public
shop does not expose; the `mock` provider ships so the whole flow runs
end-to-end, and `partner` / `session` are the real integration points. This is
a security-research / tooling exercise against the operator's own shop.
