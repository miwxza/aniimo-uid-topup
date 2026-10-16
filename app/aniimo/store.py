"""Store layer: catalogue + ordering, split into a mock and a live backend.

Ordering is split into two phases so the route can treat failures correctly:

* ``prepare_order`` — everything **before** the mutating call (mock rejection,
  openid presence, region/currency + MyCard enforcement via an upstream read).
  A failure here is safe: nothing was sent, the idempotency key is freed.
* ``dispatch_order`` — the actual ``place_order`` + response validation. A
  failure here is ambiguous: the order may exist upstream, so the key is kept
  ``unknown`` and resends are blocked until the status is checked.
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass

from app.aniimo.client import AniimoClient
from app.aniimo.idempotency import OrderResult
from app.config import Settings
from app.errors import (
    MockDataInLiveModeError,
    PaymentChannelUnavailableError,
    RegionCurrencyMismatchError,
    UpstreamBusinessError,
    UpstreamInvalidResponseError,
)
from app.models import CharacterInfo, Package


# --- helpers ----------------------------------------------------------------
def _first_pay_bonus_coin(product: dict) -> int:
    for b in product.get("benefit") or []:
        if b.get("act_type") == "level_first_rebate":
            return int(b.get("coin") or 0)
    return 0


def map_packages(products: list[dict]) -> list[Package]:
    out: list[Package] = []
    for p in products:
        out.append(
            Package(
                package_id=str(p.get("package_id")),
                product_id=str(p.get("product_id")),
                coin=int(p.get("coin") or 0),
                bonus_coin=_first_pay_bonus_coin(p),
                currency=p.get("currency") or "",
                price=float(p.get("price") or 0),
                final_price=float(p.get("final_price") or p.get("price") or 0),
                first_pay_bonus=bool(p.get("first_pay")),
                description=p.get("description"),
            )
        )
    return out


@dataclass
class PaymentContext:
    currency: str
    channels: list[str]


def assert_region_and_channel(ctx: PaymentContext, package: Package, s: Settings) -> None:
    """Hard gate: the catalogue must be TWD and MyCard must be live upstream."""
    cur = (package.currency or ctx.currency or "").upper()
    if cur != s.expected_currency.upper():
        raise RegionCurrencyMismatchError(s.expected_currency, cur or "unknown")
    if s.payment_channel_id.lower() not in [c.lower() for c in ctx.channels]:
        raise PaymentChannelUnavailableError(s.payment_channel_id)


def order_fingerprint(uid: str, server: str, package_id: str, quantity: int) -> str:
    return hashlib.sha256(f"{uid}|{server}|{package_id}|{quantity}".encode()).hexdigest()


def idempotency_key(
    uid: str, server: str, package_id: str, quantity: int, supplied: str | None
) -> str:
    if supplied:
        return supplied.strip()
    return "auto-" + order_fingerprint(uid, server, package_id, quantity)[:24]


# --- store backends ---------------------------------------------------------
class Store(abc.ABC):
    is_mock: bool

    @abc.abstractmethod
    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]: ...

    @abc.abstractmethod
    async def payment_context(self, server: str, openid: str | None = None) -> PaymentContext: ...

    async def prepare_order(
        self, character: CharacterInfo, package: Package
    ) -> None:
        """Pre-dispatch checks (no mutation). Raises on any violation."""
        if character.is_mock and not self.is_mock:
            raise MockDataInLiveModeError()
        if not character.openid:
            raise MockDataInLiveModeError()
        ctx = await self.payment_context(character.server, character.openid)
        assert_region_and_channel(ctx, package, self._settings)

    @abc.abstractmethod
    async def dispatch_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult: ...

    @property
    @abc.abstractmethod
    def _settings(self) -> Settings: ...


_MOCK_PACKAGES = [
    Package(package_id="290001", product_id="com.x.aniimos.2.1.6", coin=60, bonus_coin=60,
            currency="TWD", price=30, final_price=28, first_pay_bonus=True, description="60 Lumin"),
    Package(package_id="290002", product_id="com.x.aniimos.2.1.30", coin=300, bonus_coin=300,
            currency="TWD", price=150, final_price=138, first_pay_bonus=True, description="300 Lumin"),
    Package(package_id="290003", product_id="com.x.aniimos.2.1.98", coin=980, bonus_coin=980,
            currency="TWD", price=490, final_price=451, first_pay_bonus=True, description="980 Lumin"),
    Package(package_id="290004", product_id="com.x.aniimos.2.1.1980", coin=1980, bonus_coin=1980,
            currency="TWD", price=990, final_price=911, first_pay_bonus=True, description="1980 Lumin"),
]


class MockStore(Store):
    is_mock = True

    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def _settings(self) -> Settings:
        return self.s

    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]:
        return list(_MOCK_PACKAGES)

    async def payment_context(self, server: str, openid: str | None = None) -> PaymentContext:
        return PaymentContext(currency=self.s.expected_currency, channels=[self.s.payment_channel_id])

    async def dispatch_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult:
        oid = "MOCK-" + hashlib.sha256(idem_key.encode()).hexdigest()[:10].upper()
        return OrderResult(
            order_id=oid,
            status="created",
            currency=package.currency,
            amount=package.final_price * quantity,
            pay_url=f"https://sandbox.mycard.example/pay/{oid}",
            is_mock=True,
        )


class LiveStore(Store):
    is_mock = False

    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def _settings(self) -> Settings:
        return self.s

    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]:
        async with AniimoClient(self.s) as client:
            data = await client.game_products(server, openid)
        products = data.get("data") if isinstance(data.get("data"), list) else data
        if not isinstance(products, list):
            raise UpstreamBusinessError("n/a", "unexpected game_products shape")
        return map_packages(products)

    async def payment_context(self, server: str, openid: str | None = None) -> PaymentContext:
        async with AniimoClient(self.s) as client:
            data = await client.store_init(server, openid)
        cfg = (data.get("config") or {}).get("data") or {}
        currency = cfg.get("currency") or ""
        if not currency:
            ipc = (data.get("get_ip_currency") or {}).get("data") or {}
            currency = ipc.get("currency") or ""
        channels_raw = (data.get("channels") or {}).get("data") or []
        channels = [
            (c.get("channel_id") or c.get("channel") or "").strip()
            for c in channels_raw
            if (c.get("channel_id") or c.get("channel"))
        ]
        return PaymentContext(currency=currency, channels=channels)

    async def dispatch_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult:
        order = {
            "openid": character.openid,
            "product_id": package.product_id,
            "name": package.description or package.product_id,
            "price": package.final_price,
            "channel_id": self.s.payment_channel_id,
            "channel": self.s.payment_channel_id,
            "sub_channel_id": self.s.payment_sub_channel_id,
            "return_url": "https://www.aniimo.com/store/",
            "browser_info": {"redirect_type": "base"},
            "pay_info": {"package_id": package.package_id, "product_type": 1},
            "custom_multiple": quantity,
        }
        async with AniimoClient(self.s) as client:
            data = await client.place_order(character.server, order, idempotency_key=idem_key)
        return self._validate_order(data, package, quantity)

    def _validate_order(self, data: dict, package: Package, quantity: int) -> OrderResult:
        order_id = str(data.get("order_id") or "").strip()
        if not order_id:
            raise UpstreamInvalidResponseError("missing order_id", data)
        expected = round(package.final_price * quantity, 2)
        amt_raw = data.get("amount", data.get("price"))
        amount = float(amt_raw) if amt_raw is not None else expected
        if amt_raw is not None and round(amount, 2) != expected:
            raise UpstreamInvalidResponseError(f"amount {amount} != expected {expected}", data)
        cur = str(data.get("currency") or package.currency or "").upper()
        if cur and cur != self.s.expected_currency.upper():
            raise UpstreamInvalidResponseError(f"currency {cur}", data)
        return OrderResult(
            order_id=order_id,
            status=str(data.get("status") or "created"),
            currency=cur or package.currency,
            amount=amount,
            pay_url=data.get("pay_url") or data.get("payUrl"),
            is_mock=False,
        )


def build_store(settings: Settings) -> Store:
    return MockStore(settings) if settings.is_mock_mode else LiveStore(settings)
