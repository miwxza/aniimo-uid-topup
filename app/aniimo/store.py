"""Store flow: fetch packages and build/place UID-based orders."""
from __future__ import annotations

from app.aniimo.client import AniimoClient
from app.config import Settings
from app.errors import InvalidServerError, UpstreamBusinessError
from app.models import CharacterInfo, Package


def _first_pay_bonus_coin(product: dict) -> int:
    """Extract the 'first purchase' bonus coin from the benefit list."""
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


class StoreService:
    def __init__(self, settings: Settings):
        self.s = settings

    async def list_packages(self, server: str, openid: str | None = None) -> list[Package]:
        async with AniimoClient(self.s) as client:
            data = await client.game_products(server, openid)
        products = data.get("data") if isinstance(data.get("data"), list) else data
        if not isinstance(products, list):
            raise UpstreamBusinessError("n/a", "unexpected game_products shape")
        return map_packages(products)

    async def create_order(
        self, character: CharacterInfo, package: Package, quantity: int
    ) -> dict:
        if not character.openid:
            # Without an openid we cannot bind the order to a role.
            raise InvalidServerError(character.server)

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
            data = await client.place_order(character.server, order)
        return data
