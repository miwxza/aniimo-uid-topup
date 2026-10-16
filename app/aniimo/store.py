"""Store layer: catalogue + ordering, split into a mock and a live backend.

* ``MockStore``  — fully simulates catalogue + ordering (TWD / MyCard), so the
  whole flow runs offline for dev / CI / demo. It never touches the real API.
* ``LiveStore``  — talks to the real Aniimo API. It refuses mock character data,
  enforces TWD + MyCard **from upstream** before ordering, places the order
  non-idempotently with a dedupe guard, and validates the response against the
  proven contract before reporting success.
"""
from __future__ import annotations

import abc
import hashlib
import time
from dataclasses import dataclass, field

from app.aniimo.client import AniimoClient
from app.config import Settings
from app.errors import (
    DuplicateOrderError,
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
    channels: list[str]  # lower-cased channel ids available from upstream


@dataclass
class OrderResult:
    order_id: str
    status: str  # created | pending | unknown
    currency: str
    amount: float
    pay_url: str | None
    is_mock: bool = False


def assert_region_and_channel(ctx: PaymentContext, package: Package, s: Settings) -> None:
    """Hard gate: the catalogue must be TWD and MyCard must be live upstream."""
    cur = (package.currency or ctx.currency or "").upper()
    if cur != s.expected_currency.upper():
        raise RegionCurrencyMismatchError(s.expected_currency, cur or "unknown")
    if s.payment_channel_id.lower() not in [c.lower() for c in ctx.channels]:
        raise PaymentChannelUnavailableError(s.payment_channel_id)


def idempotency_key(uid: str, server: str, package_id: str, quantity: int, supplied: str | None) -> str:
    if supplied:
        return supplied.strip()
    raw = f"{uid}|{server}|{package_id}|{quantity}"
    return "auto-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


# --- in-memory idempotency / dedupe guard -----------------------------------
@dataclass
class _Entry:
    status: str  # in_flight | done
    result: OrderResult | None
    ts: float


class OrderGuard:
    """Prevents double submission: an identical key in flight is rejected, and a
    recently completed key returns the cached result instead of re-ordering.

    In-memory (single node). Back with Redis for multi-node — same contract.
    """

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._store: dict[str, _Entry] = {}

    def _gc(self, now: float) -> None:
        for k in [k for k, e in self._store.items() if now - e.ts > self.ttl]:
            self._store.pop(k, None)

    def begin(self, key: str) -> OrderResult | None:
        now = time.monotonic()
        self._gc(now)
        e = self._store.get(key)
        if e is not None:
            if e.status == "in_flight":
                raise DuplicateOrderError(key)
            if e.status == "done" and e.result is not None:
                return e.result  # replay cached result
        self._store[key] = _Entry(status="in_flight", result=None, ts=now)
        return None

    def complete(self, key: str, result: OrderResult) -> None:
        self._store[key] = _Entry(status="done", result=result, ts=time.monotonic())

    def abort(self, key: str) -> None:
        self._store.pop(key, None)


# --- store backends ---------------------------------------------------------
class Store(abc.ABC):
    is_mock: bool

    @abc.abstractmethod
    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]: ...

    @abc.abstractmethod
    async def payment_context(self, server: str, openid: str | None = None) -> PaymentContext: ...

    @abc.abstractmethod
    async def create_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult: ...


# Mock catalogue mirrors the reference screenshot (TWD, first-buy bonus).
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

    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]:
        return list(_MOCK_PACKAGES)

    async def payment_context(self, server: str, openid: str | None = None) -> PaymentContext:
        return PaymentContext(currency=self.s.expected_currency, channels=[self.s.payment_channel_id])

    async def create_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult:
        ctx = await self.payment_context(character.server)
        assert_region_and_channel(ctx, package, self.s)  # exercised even in mock
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
        currency = ""
        cfg = (data.get("config") or {}).get("data") or {}
        currency = cfg.get("currency") or ""
        if not currency:
            ipc = (data.get("get_ip_currency") or {}).get("data") or {}
            currency = ipc.get("currency") or ""
        channels_raw = (data.get("channels") or {}).get("data") or []
        channels: list[str] = []
        for c in channels_raw:
            cid = (c.get("channel_id") or c.get("channel") or "").strip()
            if cid:
                channels.append(cid)
        return PaymentContext(currency=currency, channels=channels)

    async def create_order(
        self, character: CharacterInfo, package: Package, quantity: int, idem_key: str
    ) -> OrderResult:
        if character.is_mock:
            raise MockDataInLiveModeError()
        if not character.openid:
            raise MockDataInLiveModeError()

        # Gate on real upstream region + channel availability.
        ctx = await self.payment_context(character.server, character.openid)
        assert_region_and_channel(ctx, package, self.s)

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
        """Only report success when the response actually carries a usable order."""
        order_id = str(data.get("order_id") or "").strip()
        if not order_id:
            raise UpstreamInvalidResponseError("missing order_id", data)
        expected = round(package.final_price * quantity, 2)
        amt_raw = data.get("amount", data.get("price"))
        amount = float(amt_raw) if amt_raw is not None else expected
        if amt_raw is not None and round(amount, 2) != expected:
            raise UpstreamInvalidResponseError(
                f"amount {amount} != expected {expected}", data
            )
        cur = str(data.get("currency") or package.currency or "").upper()
        if cur and cur != self.s.expected_currency.upper():
            raise UpstreamInvalidResponseError(f"currency {cur}", data)
        pay_url = data.get("pay_url") or data.get("payUrl")
        status = str(data.get("status") or "created")
        return OrderResult(
            order_id=order_id,
            status=status,
            currency=cur or package.currency,
            amount=amount,
            pay_url=pay_url,
            is_mock=False,
        )


def build_store(settings: Settings) -> Store:
    return MockStore(settings) if settings.is_mock_mode else LiveStore(settings)
