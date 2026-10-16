"""End-to-end tests against the FastAPI app with upstream mocked via respx."""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

STORE = "https://worldx-store-coin.kingsgroupgames.com"

_PRODUCTS = {
    "code": 0,
    "data": {
        "code": 0,
        "data": [
            {
                "package_id": "290002",
                "product_id": "com.x.aniimos.2.1.30",
                "coin": 300,
                "currency": "TWD",
                "price": 138,
                "final_price": 138,
                "first_pay": True,
                "description": "300 Lumin",
                "benefit": [{"act_type": "level_first_rebate", "coin": 300}],
            }
        ],
    },
}


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    monkeypatch.setenv("ANIIMO_ROLE_PROVIDER", "mock")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_verify_ok(client):
    r = client.post("/api/v1/aniimo/verify", json={"uid": "412000126173", "server": "4"})
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True
    assert body["character"]["role_name"] == "Sweettoast"
    assert body["source"] == "mock"


def test_verify_unknown_uid(client):
    r = client.post("/api/v1/aniimo/verify", json={"uid": "999999999", "server": "4"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "UID_NOT_FOUND"


def test_verify_bad_uid_validation(client):
    r = client.post("/api/v1/aniimo/verify", json={"uid": "abc", "server": "4"})
    assert r.status_code == 422


@respx.mock
def test_packages(client):
    respx.post(f"{STORE}/api/game_products").mock(
        return_value=httpx.Response(200, json=_PRODUCTS)
    )
    r = client.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "TWD"
    assert body["packages"][0]["package_id"] == "290002"


@respx.mock
def test_order_flow(client):
    respx.post(f"{STORE}/api/game_products").mock(
        return_value=httpx.Response(200, json=_PRODUCTS)
    )
    respx.post(f"{STORE}/api/place_order").mock(
        return_value=httpx.Response(
            200,
            json={"code": 0, "data": {"code": 0, "order_id": "ORD123", "status": "pending", "pay_url": "https://pay.mycard.example/ORD123"}},
        )
    )
    r = client.post(
        "/api/v1/aniimo/order",
        json={"uid": "412000126173", "server": "4", "package_id": "290002", "quantity": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_id"] == "ORD123"
    assert body["amount"] == 138
    assert body["pay_url"].startswith("https://pay.mycard")


@respx.mock
def test_order_upstream_risk_block(client):
    respx.post(f"{STORE}/api/game_products").mock(
        return_value=httpx.Response(200, json=_PRODUCTS)
    )
    respx.post(f"{STORE}/api/place_order").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {"code": 106042, "message": "too frequent"}})
    )
    r = client.post(
        "/api/v1/aniimo/order",
        json={"uid": "412000126173", "server": "4", "package_id": "290002"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RISK_CONTROL_BLOCK"


@respx.mock
def test_upstream_ip_block(client):
    respx.post(f"{STORE}/api/game_products").mock(return_value=httpx.Response(403, text="forbidden"))
    r = client.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "IP_BLOCKED"


@respx.mock
def test_upstream_captcha(client):
    respx.post(f"{STORE}/api/game_products").mock(
        return_value=httpx.Response(200, html="<html>captcha challenge</html>")
    )
    r = client.get("/api/v1/aniimo/packages", params={"server": "4"})
    assert r.status_code == 423
    assert r.json()["error"]["code"] == "CAPTCHA_REQUIRED"
