"""End-to-end + unit tests.

Mock-mode tests exercise the fully simulated flow. Live-mode tests drive the
real code paths with the upstream Aniimo API mocked via respx (partner provider
for character lookup, store endpoints for catalogue/payment/order).
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings

STORE = "https://worldx-store-coin.kingsgroupgames.com"
PARTNER = "https://partner.example.test/lookup"


def _products(currency="TWD"):
    return {
        "code": 0,
        "data": {
            "code": 0,
            "data": [
                {
                    "package_id": "290002",
                    "product_id": "com.x.aniimos.2.1.30",
                    "coin": 300,
                    "currency": currency,
                    "price": 150,
                    "final_price": 138,
                    "first_pay": True,
                    "description": "300 Lumin",
                    "benefit": [{"act_type": "level_first_rebate", "coin": 300}],
                }
            ],
        },
    }


def _store_init(currency="TWD", channels=("mycard",)):
    return {
        "code": 0,
        "data": {
            "channels": {"code": 0, "data": [{"channel_id": c} for c in channels]},
            "config": {"code": 0, "data": {"currency": currency}},
            "game_products": {"code": 0, "data": []},
            "get_ip_currency": {"code": 0, "data": {"currency": currency}},
            "user_pay_info": {"code": 0, "data": {}},
        },
    }


def _partner_role():
    return httpx.Response(
        200,
        json={"data": {"role_name": "Sweettoast", "level": 80, "openid": "oid_real_123"}},
    )


def _order_ok(order_id="ORD123", **extra):
    base = {"order_id": order_id, "status": "pending", "pay_url": "https://pay.mycard.example/" + order_id}
    base.update(extra)
    return {"code": 0, "data": {"code": 0, **base}}


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("ANIIMO_ROLE_PROVIDER", "mock")
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setenv("ANIIMO_ROLE_PROVIDER", "partner")
    monkeypatch.setenv("ANIIMO_PARTNER_VERIFY_URL", PARTNER)
    monkeypatch.setenv("ANIIMO_BREAKER_THRESHOLD", "1")
    get_settings.cache_clear()
    from app.main import app
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


# --- mock mode --------------------------------------------------------------
def test_health(mock_mode):
    assert mock_mode.get("/health").json()["status"] == "ok"


def test_verify_mock_is_flagged(mock_mode):
    r = mock_mode.post("/api/v1/aniimo/verify", json={"uid": "412000126173", "server": "4"})
    b = r.json()
    assert r.status_code == 200
    assert b["character"]["is_mock"] is True
    assert b["note"]  # explicitly labelled as not-live


def test_verify_unknown(mock_mode):
    r = mock_mode.post("/api/v1/aniimo/verify", json={"uid": "999999999", "server": "4"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "UID_NOT_FOUND"


def test_verify_bad_uid(mock_mode):
    assert mock_mode.post("/api/v1/aniimo/verify", json={"uid": "abc", "server": "4"}).status_code == 422


def test_mock_packages_are_twd(mock_mode):
    r = mock_mode.get("/api/v1/aniimo/packages", params={"server": "4"})
    b = r.json()
    assert b["currency"] == "TWD"
    assert {p["package_id"] for p in b["packages"]} >= {"290001", "290002"}
    assert all(p["currency"] == "TWD" for p in b["packages"])


def test_mock_order_is_simulated(mock_mode):
    r = mock_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    b = r.json()
    assert r.status_code == 200
    assert b["is_mock"] is True and b["order_id"].startswith("MOCK-")
    assert b["amount"] == 138 and b["currency"] == "TWD"


def test_mock_product_not_found(mock_mode):
    r = mock_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "999999"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


# --- live mode --------------------------------------------------------------
@respx.mock
def test_live_order_success(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init()))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))
    respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(200, json=_order_ok(amount=138, currency="TWD")))
    r = live_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["order_id"] == "ORD123" and b["is_mock"] is False
    assert b["amount"] == 138


@respx.mock
def test_live_currency_mismatch(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init(currency="THB")))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products(currency="THB")))
    r = live_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REGION_CURRENCY_MISMATCH"


@respx.mock
def test_live_channel_unavailable(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init(channels=("adyen",))))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))
    r = live_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAYMENT_CHANNEL_UNAVAILABLE"


@respx.mock
def test_live_order_timeout_is_unknown_not_resent(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init()))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))
    route = respx.post(f"{STORE}/api/place_order").mock(side_effect=httpx.ReadTimeout("boom"))
    r = live_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    assert r.status_code == 202 and r.json()["error"]["code"] == "ORDER_RESULT_UNKNOWN"
    assert route.call_count == 1  # NOT auto-resent


@respx.mock
def test_live_invalid_response(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init()))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))
    # order "ok" but no order_id → must not be reported as created
    respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(200, json={"code": 0, "data": {"code": 0, "status": "ok"}}))
    r = live_mode.post("/api/v1/aniimo/order",
                       json={"uid": "412000126173", "server": "4", "package_id": "290002"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"


def _live_order_routes():
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init()))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))


@respx.mock
def test_live_duplicate_order_blocked(live_mode):
    _live_order_routes()
    respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(200, json=_order_ok()))
    payload = {"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "k-1"}
    first = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert first.status_code == 200
    # Same key + same data → replayed cached result, not a second place_order
    second = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert second.status_code == 200 and second.json()["order_id"] == "ORD123"
    po = [c for c in respx.calls if c.request.url.path == "/api/place_order"]
    assert len(po) == 1


@respx.mock
def test_idempotency_key_reuse_with_different_data_conflicts(live_mode):
    _live_order_routes()
    respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(200, json=_order_ok()))
    base = {"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "k-2"}
    assert live_mode.post("/api/v1/aniimo/order", json={**base, "quantity": 1}).status_code == 200
    # Same key, different quantity → must NOT replay the old order.
    r = live_mode.post("/api/v1/aniimo/order", json={**base, "quantity": 2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


@respx.mock
def test_post_send_invalid_response_blocks_resend(live_mode):
    _live_order_routes()
    route = respx.post(f"{STORE}/api/place_order").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"code": 0, "status": "ok"}})
    )
    payload = {"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "k-3"}
    first = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert first.status_code == 502 and first.json()["error"]["code"] == "UPSTREAM_INVALID_RESPONSE"
    # Resend of the same order must NOT hit place_order again (outcome unknown).
    second = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert second.status_code == 202 and second.json()["error"]["code"] == "ORDER_RESULT_UNKNOWN"
    assert route.call_count == 1


@respx.mock
def test_post_send_500_blocks_resend(live_mode):
    _live_order_routes()
    route = respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(500, json={"code": 0, "data": {}}))
    payload = {"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "k-4"}
    first = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert first.status_code == 502
    second = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert second.status_code == 202 and second.json()["error"]["code"] == "ORDER_RESULT_UNKNOWN"
    assert route.call_count == 1


@respx.mock
def test_order_breaker_opens_on_consecutive_order_failures(live_mode):
    # live_mode sets ANIIMO_BREAKER_THRESHOLD=1 → one order fault opens it.
    _live_order_routes()
    route = respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(500, json={"code": 0, "data": {}}))
    # Distinct keys so the dedupe guard does not mask the breaker behaviour.
    r1 = live_mode.post("/api/v1/aniimo/order",
                        json={"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "b-1"})
    assert r1.status_code == 502
    # Reads still succeed, but the order-stage circuit is now open → short-circuit.
    r2 = live_mode.post("/api/v1/aniimo/order",
                        json={"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "b-2"})
    assert r2.status_code == 502
    assert route.call_count == 1  # 2nd order never reached place_order


@respx.mock
def test_live_ip_block(live_mode):
    respx.post(PARTNER).mock(return_value=_partner_role())
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(403, text="forbidden"))
    r = live_mode.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "IP_BLOCKED"


@respx.mock
def test_read_breaker_opens_after_upstream_fault(live_mode):
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(500, json={"code": 0, "data": {}}))
    r1 = live_mode.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r1.status_code == 502
    # threshold=1 → circuit now open; next call short-circuits without hitting upstream
    r2 = live_mode.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r2.status_code == 502


# --- unit: live store rejects mock character --------------------------------
@pytest.mark.asyncio
async def test_livestore_refuses_mock_character():
    from app.aniimo.store import LiveStore
    from app.config import Settings
    from app.errors import MockDataInLiveModeError
    from app.models import CharacterInfo, Package

    s = Settings(role_provider="partner", partner_verify_url=PARTNER)
    store = LiveStore(s)
    char = CharacterInfo(uid="412000126173", server="4", role_name="x", openid="MOCK-x", is_mock=True)
    pkg = Package(package_id="290002", product_id="p", coin=300, currency="TWD", price=150, final_price=138)
    with pytest.raises(MockDataInLiveModeError):
        await store.prepare_order(char, pkg)


@respx.mock
def test_replay_ignores_provider_outage_and_keeps_done(live_mode):
    # First order succeeds; a later provider outage must NOT re-place the order
    # nor delete the cached 'done' entry.
    respx.post(PARTNER).mock(side_effect=[_partner_role(), httpx.Response(500, text="down")])
    respx.post(f"{STORE}/api/store/init").mock(return_value=httpx.Response(200, json=_store_init()))
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(200, json=_products()))
    po = respx.post(f"{STORE}/api/place_order").mock(return_value=httpx.Response(200, json=_order_ok()))
    payload = {"uid": "412000126173", "server": "4", "package_id": "290002", "idempotency_key": "r-1"}
    first = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert first.status_code == 200
    # Resend while provider is down → must replay cached result without calling
    # the provider or place_order again.
    second = live_mode.post("/api/v1/aniimo/order", json=payload)
    assert second.status_code == 200 and second.json()["order_id"] == "ORD123"
    assert second.json()["character"]["role_name"] == "Sweettoast"
    partner_calls = [c for c in respx.calls if c.request.url.host == "partner.example.test"]
    assert len(partner_calls) == 1  # provider hit once (first order only)
    assert po.call_count == 1       # order placed exactly once


# --- unit: durable guard survives a 'restart' (sqlite) ----------------------
def test_sqlite_guard_persists_unknown_across_restart(tmp_path):
    from app.aniimo.idempotency import OrderGuard, SqliteStateStore
    from app.errors import OrderResultUnknownError

    db = str(tmp_path / "state.db")
    g1 = OrderGuard(SqliteStateStore(db))
    assert g1.begin("k", "fp") is None
    g1.mark_unknown("k", "fp")
    # Simulate a process restart: new guard, same db file.
    g2 = OrderGuard(SqliteStateStore(db))
    with pytest.raises(OrderResultUnknownError):
        g2.begin("k", "fp")  # unknown state is still there, resend blocked


def test_sqlite_migrates_old_schema_without_data_loss(tmp_path):
    # Reproduce an older db (round-2 schema, no 'character' column) holding an
    # 'unknown' dedupe entry and a legacy 'done' order, then open it with the
    # current store: it must ALTER (not crash) and keep both rows.
    import sqlite3
    import time

    from app.aniimo.idempotency import OrderGuard, SqliteStateStore
    from app.errors import OrderResultUnknownError

    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE order_state ("
        "key TEXT PRIMARY KEY, status TEXT, fingerprint TEXT, result TEXT, ts REAL)"
    )
    now = time.time()
    con.execute("INSERT INTO order_state VALUES ('u','unknown','fp1',NULL,?)", (now,))
    con.execute(
        "INSERT INTO order_state VALUES ('d','done','fp2',?,?)",
        ('{"order_id":"OLD1","status":"created","currency":"TWD","amount":138.0,"pay_url":null,"is_mock":false}', now),
    )
    con.commit()
    con.close()

    g = OrderGuard(SqliteStateStore(db))  # must migrate, not raise OperationalError

    # Unknown dedupe history preserved → resend still blocked.
    with pytest.raises(OrderResultUnknownError):
        g.begin("u", "fp1")

    # Legacy 'done' preserved and replayable; character is simply absent.
    entry = g.begin("d", "fp2")
    assert entry is not None and entry.result.order_id == "OLD1"
    assert entry.character is None

    # Writing a NEW entry into the migrated db works (the ALTER added 'character').
    assert g.begin("n", "fp3") is None
    g.mark_unknown("n", "fp3")
    with pytest.raises(OrderResultUnknownError):
        g.begin("n", "fp3")


def test_sqlite_reserve_is_atomic_single_winner(tmp_path):
    # Two guards (two 'workers') on one db: only one may win the same key.
    from app.aniimo.idempotency import OrderGuard, SqliteStateStore
    from app.errors import DuplicateOrderError

    db = str(tmp_path / "state.db")
    g1 = OrderGuard(SqliteStateStore(db))
    g2 = OrderGuard(SqliteStateStore(db))
    assert g1.begin("k", "fp") is None            # g1 wins the slot
    with pytest.raises(DuplicateOrderError):
        g2.begin("k", "fp")                       # g2 is rejected, not admitted


def test_prod_plus_mock_fails_fast():
    from app.config import Settings
    with pytest.raises(Exception):
        Settings(env="prod", role_provider="mock")


def test_prod_requires_durable_backend():
    from app.config import Settings
    with pytest.raises(Exception):
        Settings(env="prod", role_provider="partner",
                 partner_verify_url=PARTNER, idempotency_backend="memory")
    # sqlite in prod is accepted
    s = Settings(env="prod", role_provider="partner",
                 partner_verify_url=PARTNER, idempotency_backend="sqlite")
    assert s.idempotency_backend == "sqlite"
