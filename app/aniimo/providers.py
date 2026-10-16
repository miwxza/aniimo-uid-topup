"""RoleProvider abstraction: resolve a (uid, server) -> CharacterInfo.

The official Aniimo webshop exposes **no public UID-lookup endpoint**. Character
data is only available via ``/api/player/roles`` scoped to the logged-in
account. Converting "Via Login" to "UID Top-Up" therefore requires an
out-of-band source for arbitrary-UID verification. We make that source a
pluggable strategy so the order flow (which is real) stays decoupled from it.

Providers:
    PartnerRoleProvider  - production path: a publisher partner UID-lookup API.
    SessionRoleProvider  - bridge through one authenticated store-account session
                           (only resolves that account's own roles).
    MockRoleProvider     - deterministic fixtures for local dev / CI / demo.
"""
from __future__ import annotations

import abc

import httpx

from app.aniimo.client import AniimoClient
from app.config import Settings
from app.errors import InvalidUidError, UpstreamUnavailableError
from app.models import CharacterInfo

# Demo fixtures mirror the assignment's example screenshot.
_MOCK_ROLES: dict[tuple[str, str], CharacterInfo] = {
    ("412000126173", "4"): CharacterInfo(
        uid="412000126173",
        server="4",
        role_name="Sweettoast",
        level=80,
        openid="oid_412000126173",
        avatar_url=None,
    ),
}


class RoleProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def resolve(self, uid: str, server: str) -> CharacterInfo: ...


class MockRoleProvider(RoleProvider):
    name = "mock"

    async def resolve(self, uid: str, server: str) -> CharacterInfo:
        info = _MOCK_ROLES.get((uid, server))
        if not info:
            raise InvalidUidError(uid, server)
        return info


class PartnerRoleProvider(RoleProvider):
    """Calls a publisher-provided partner endpoint:  POST {uid, server} -> role."""

    name = "partner"

    def __init__(self, settings: Settings):
        self.s = settings
        if not settings.partner_verify_url:
            raise RuntimeError("ANIIMO_PARTNER_VERIFY_URL is required for role_provider=partner")

    async def resolve(self, uid: str, server: str) -> CharacterInfo:
        headers = {"Content-Type": "application/json"}
        if self.s.partner_api_key:
            headers["Authorization"] = f"Bearer {self.s.partner_api_key}"
        try:
            async with httpx.AsyncClient(
                timeout=self.s.http_timeout, proxy=self.s.outbound_proxy
            ) as c:
                r = await c.post(
                    self.s.partner_verify_url,
                    headers=headers,
                    json={"uid": uid, "server": server},
                )
        except httpx.HTTPError as e:
            raise UpstreamUnavailableError(f"partner lookup failed: {e}") from e

        if r.status_code == 404:
            raise InvalidUidError(uid, server)
        if r.status_code >= 400:
            raise UpstreamUnavailableError(f"partner lookup status {r.status_code}")
        d = r.json().get("data", r.json())
        name = d.get("role_name") or d.get("roleName")
        if not name:
            raise InvalidUidError(uid, server)
        return CharacterInfo(
            uid=uid,
            server=server,
            role_name=name,
            level=d.get("level"),
            openid=d.get("openid") or d.get("openId"),
            avatar_url=d.get("avatar_url") or d.get("avatar"),
        )


class SessionRoleProvider(RoleProvider):
    """Resolves roles belonging to one authenticated store-account session.

    Useful when the operator runs top-ups for their own linked characters; it
    cannot verify arbitrary customer UIDs (the shop never exposes that).
    """

    name = "session"

    def __init__(self, settings: Settings):
        self.s = settings
        if not settings.account_session_cookie:
            raise RuntimeError("ANIIMO_ACCOUNT_SESSION_COOKIE is required for role_provider=session")

    async def resolve(self, uid: str, server: str) -> CharacterInfo:
        async with AniimoClient(self.s) as client:
            data = await client.player_roles(self.s.account_session_cookie)
        roles = data.get("roles") or []
        for role in roles:
            if str(role.get("roleId")) == uid and str(role.get("serverArea")) == server:
                return CharacterInfo(
                    uid=uid,
                    server=server,
                    role_name=role.get("roleName") or role.get("roleId"),
                    level=role.get("level"),
                    openid=role.get("openId") or role.get("openid"),
                    avatar_url=role.get("avatar") or role.get("avatarUrl"),
                )
        raise InvalidUidError(uid, server)


def build_provider(settings: Settings) -> RoleProvider:
    match settings.role_provider:
        case "partner":
            return PartnerRoleProvider(settings)
        case "session":
            return SessionRoleProvider(settings)
        case _:
            return MockRoleProvider()
