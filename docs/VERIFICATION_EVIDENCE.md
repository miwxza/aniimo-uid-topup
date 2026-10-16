# Character verification evidence — 2026-10-16

## Confirmed account flow

```text
Email/OTP or supported login
  -> office API issues Bearer credential
  -> GET /api/player/roles with Authorization
  -> code=200, data.roles[]
  -> compare roleId + serverArea locally
```

The account endpoint request does not carry a UID query or request body in the observed browser capture. UID-only lookup is not established by receiving a role list after login.

| Endpoint | Method | Authentication | Purpose / evidence |
|---|---|---|---|
| `https://worldx-office-api.aniimo.com/api/player/roles` | GET | Bearer | Browser capture and SDK; returns the authenticated account's roles |
| `/api/player/roles/refresh` on office host | POST | Bearer | SDK loads the same account roles after refresh; not used to query arbitrary UID |
| `/api/player/roles/default` on office host | PUT | Bearer | SDK selects role using targetClusterId and roleId; changes account preference, not called during research |
| `/api/player/login` on office host | POST | Email/OTP or supported flow | SDK exchanges login evidence for token; no credentials included here |
| `/api/account/store/u` on legacy store-account host | POST | Account bridge data | Store bundle reference; not proven to be arbitrary UID lookup |
| `/api/store/init`, `/api/game_products` on store-coin host | POST | Public envelope; optional role openid | Public catalogue reads; not character-name lookup |

## Evidence files and sources

Public SDK:
`https://worldx-website-cdn.aniimo.com/official-website/worldx/office_global/login_sdk/worldx-login-dialog-1.1.5.js`

SHA256 of inspected SDK bytes (UTF-8 text re-encoding):
`9ae11dd3247fcd0e901698d8d58fb0d2bcbe61ef0cd1e949080e4f06d19daf39`

Store bundle:
`https://kg-web-cdn.akamaized.net/main/user-platform-web/worldx-web-store-global/online/global/assets/chunks/main-B4OOgSkN.js`

SHA256 of inspected bundle bytes (UTF-8 text re-encoding):
`45a01229e90352aeac364d5121f579669a1f4d13baf8b93b0d959115c7287ac8`

SDK observations:

- fetch headers include `Authorization: Bearer ${token}` when configured.
- fetch options use `credentials: omit`, `cache: no-store`.
- success test is JSON `code === 200`.
- saved roles: GET `/api/player/roles`; refresh: POST `/api/player/roles/refresh`.
- store login redirects append UID **alongside** `session_key` or `fp_session_key`. That is credential forwarding/role selection, not password-free UID authentication.

## Recorded checks

1. Unauthenticated GET office roles -> HTTP 401, `{"code":401,"msg":null,"data":null}`.
2. User's authorized session capture -> code 200, UID `412001104638`, roleName `Miwx`, serverArea 4, openid present. Credential and internal account identifiers omitted.
3. Local API on the user-configured session server -> HTTP 200, source=session, role_name=Miwx, is_mock=false. No credential was extracted from the server or printed.
4. A synthetic unmatched UID against the local session API -> prior version returned UID_NOT_FOUND. Current code corrects this to UID_NOT_IN_ACCOUNT, because global existence cannot be inferred.
5. Public store/init: numeric server_id=4 works; string "4" returned 106000. Current client emits JSON integer.

## What is NOT proven

- No authorized arbitrary-UID verification contract has been identified in the inspected maps.
- This limited source inspection is not a proof that the publisher has no other API.
- No encryption/signature bypass or token derivation is implemented or claimed.
- No production order or payment was submitted.
- PartnerRoleProvider remains an integration interface tested with synthetic responses.

## Next dependency

Obtain an authorized UID+Server lookup contract or an additional legitimate flow from the assessment owner/publisher. Validate with an authorized test UID outside the operator account before claiming arbitrary UID verification is complete. Taiwan egress is relevant to checkout currency, not a substitute for this dependency.
