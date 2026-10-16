# Reverse Engineering — Aniimo Webshop

Target: `https://www.aniimo.com/store/` (global region). Goal: understand the
top-up flow well enough to drive it by **UID only** (no customer password),
locked to **Taiwan / TWD / MyCard**.

## 1. Method

1. Loaded the store SPA and captured all non-asset resources via the
   `performance.getEntriesByType('resource')` timeline and the network panel.
2. Pulled the production JS chunks from the CDN
   (`kg-web-cdn.akamaized.net/.../worldx-web-store-global/.../assets/chunks/`),
   plus the login SDK (`worldx-login-dialog-1.1.5.js`), and prettified them.
3. Read the request/response layer (`request-*.js`), the endpoint map
   (`payment-*.js`), the store/order state machine (`main-*.js`), and the
   login/roles flow (`worldx-login-dialog-*.js`).
4. Confirmed behaviour with live calls to the unauthenticated endpoints.

## 2. Hosts & endpoint map

| Purpose | Host | Path |
|---|---|---|
| Store catalogue / order | `worldx-store-coin.kingsgroupgames.com` | `/api/store/init`, `/api/game_products`, `/api/store/cal_show`, `/api/store/cal_final`, `/api/place_order`, `/api/preload_order`, `/api/order` |
| Account / roles | `worldx-store-account.kingsgroupgames.com` | `/api/player/login`, `/api/player/roles`, `/api/player/roles/refresh`, `/api/account/store/u` |
| Login SDK (email OTP, social) | served from CDN | `/api/player/email`, `/api/player/mobile`, `/api/player/login` |

Region config (from `payment-*.js`) for the global entity:

```
officialUrl        https://www.aniimo.com
accountApiBaseUrl  https://worldx-store-account.kingsgroupgames.com
storeApiBaseUrl    https://worldx-store-coin.kingsgroupgames.com
ameApiBaseUrl      https://worldx-ame.kingsgroupgames.com
```

## 3. Request contract

**Common envelope** (added by a request interceptor, `xe()` in `request-*.js`)
to every store call:

```json
{ "game_id": 999002, "game_project": "worldx_global", "source": "web", "server_id": <n> }
```

`game_id` 999002 = `worldx_global` (rawprint entity); 999003 = funverse.
`server_id` defaults to the selected role's `serverArea`, else `1`.

**No signature / HMAC / encryption** is applied to any store payload. There is
no `sign`, nonce, or timestamp field. The only integrity surface is:

- A ShuMei (`sm.min.js`) device-fingerprint token used by the **login** flow
  (and China-Mobile phone auth), *not* by the store order payload.
- `fp_device_id` / `browser_id` attached to `place_order` for risk scoring,
  which are advisory, not cryptographic.

**Response envelope** is doubly wrapped:

```json
{ "code": 0, "data": { "code": 0, "data": <payload>, "message": "ok" } }
```

The outer `code` is transport-level; a non-zero **inner** `code` is a business
error (e.g. `106042` = order too frequent). Both layers are normalised in
`app/aniimo/client.py::_unwrap`.

## 4. Identity model — the key finding

The store identifies a player by **`openid`**, derived from the selected role
(`role.openId`). Ordering is openid-bound:

```js
// main-*.js, prepareOrderParams()
{ openid, product_id, payment_appid, name, return_url, price,
  channel_id, channel, sub_channel_id,
  pay_info: { package_id, product_type }, custom_multiple: quantity, ... }
```

`/api/place_order` **trusts the openid** — there is no per-order token. So once
you hold a valid `openid` for a target role, you can create an order for it
without that user's credentials. That is what makes a UID top-up flow possible
at all.

### Where character data comes from

Character name / server / openid are only returned by
`GET /api/player/roles`, which lists the roles of the **currently logged-in
account** (session cookie set by `/api/player/login`). Role shape:

```
{ targetClusterId, roleId (=UID), roleName, serverArea, openId, isDefault, avatar }
```

There is **no public "lookup any UID → character" endpoint**. The `ame` host
and the `uaid*` calls in `sm.min.js` are device/phone-auth, not role lookup.

### Consequence for "Via Login → UID Top-Up"

To verify an **arbitrary** customer UID (as in the Midasbuy-style reference
flow) you need a source the public shop does not expose:

- a **publisher partner UID-lookup API** (the production answer), or
- an authenticated **store-account session** that can only see its own roles.

The service makes this a pluggable `RoleProvider` (`app/aniimo/providers.py`)
so the real, working order flow (openid → `place_order`) is decoupled from how
the openid/name is obtained.

## 5. Currency & MyCard

`store/init` returns `get_ip_currency` — **currency is chosen by caller IP**.
From a Thailand IP the live catalogue returns `THB`. To obtain `TWD` pricing
and the MyCard channel the upstream calls must egress from a **Taiwan IP**
(`ANIIMO_OUTBOUND_PROXY`). The payment channel is pinned to MyCard in config
(`payment_channel_id = "mycard"`).

## 6. Live verification performed

- `POST /api/store/init` (unauthenticated) → 200, full config + `game_products`.
- `POST /api/game_products` (unauthenticated) → 200, 9 packages parsed
  (60/300/980/1980/3280/6480 Lumin + 3 passes), confirming the catalogue is
  readable without login.

### Evidence (redacted)

Request (from a Thailand IP; currency therefore `THB`, not `TWD`):

```http
POST /api/game_products HTTP/1.1
Host: worldx-store-coin.kingsgroupgames.com
Content-Type: application/json

{"game_id":999002,"game_project":"worldx_global","source":"web","server_id":1}
```

Response (trimmed to one item):

```json
{"code":0,"data":{"code":0,"message":"ok","data":[
  {"package_id":"290002","product_id":"com.x.aniimos.2.1.30","coin":300,
   "currency":"THB","price":199,"final_price":184,"first_pay":true,
   "benefit":[{"act_type":"level_first_rebate","coin":300,"discount":1}]}
]}}
```

This confirms: unauthenticated read, the `{code:0,data:{code:0,data:[…]}}`
double envelope, IP-derived currency, and the package shape the service maps.
Only the unauthenticated catalogue was exercised live; `place_order` was **not**
fired against production (it is covered by tests with a mocked upstream).
