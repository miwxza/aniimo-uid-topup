"""Pydantic request/response schemas for the public API."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


# --- /verify ---------------------------------------------------------------
class VerifyRequest(BaseModel):
    uid: str = Field(..., examples=["412000126173"], description="In-game player UID.")
    server: str = Field(..., examples=["4"], description="Server / server_area id.")

    @field_validator("uid")
    @classmethod
    def _uid_numeric(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or not (4 <= len(v) <= 20):
            raise ValueError("uid must be 4-20 digits")
        return v

    @field_validator("server")
    @classmethod
    def _server_clean(cls, v: str) -> str:
        return v.strip()


class CharacterInfo(BaseModel):
    uid: str
    server: str
    role_name: str
    level: int | None = None
    openid: str | None = Field(
        default=None,
        description="Store-side identity used for ordering. Omitted unless resolvable.",
    )
    avatar_url: str | None = None


class VerifyResponse(BaseModel):
    verified: bool
    character: CharacterInfo
    source: str = Field(description="Which RoleProvider resolved the character.")


# --- /packages -------------------------------------------------------------
class Package(BaseModel):
    package_id: str
    product_id: str
    coin: int
    bonus_coin: int = 0
    currency: str
    price: float
    final_price: float
    first_pay_bonus: bool = False
    description: str | None = None


class PackagesResponse(BaseModel):
    currency: str
    payment_channel: str
    packages: list[Package]


# --- /order ----------------------------------------------------------------
class CreateOrderRequest(BaseModel):
    uid: str
    server: str
    package_id: str = Field(..., examples=["290002"])
    quantity: int = Field(default=1, ge=1, le=99)

    @field_validator("uid")
    @classmethod
    def _uid_numeric(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("uid must be numeric")
        return v


class OrderResponse(BaseModel):
    order_id: str
    status: str
    currency: str
    amount: float
    payment_channel: str
    pay_url: str | None = Field(
        default=None, description="Hosted MyCard checkout URL to redirect the payer to."
    )
    character: CharacterInfo


class ErrorBody(BaseModel):
    code: str
    message: str
    retry_after: int | None = None
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
