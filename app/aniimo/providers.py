"""RoleProvider abstraction: resolve a (uid, server) -> CharacterInfo.

No arbitrary-UID lookup was found in the inspected public bundles. The
observed ``/api/player/roles`` is scoped to the logged-in account. Converting "Via Login" to "UID Top-Up" therefore requires an
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
from app.errors import InvalidUidError, UidNotInAccountError, UpstreamUnavailableError
from app.models import CharacterInfo

# Demo fixtures mirror the assignment's example screenshot. These are clearly
# flagged mock records (is_mock=True) and the openid is a *fixture*, not derived
# from the UID — LiveStore refuses to order from them.
_MOCK_ROLES: dict[tuple[str, str], CharacterInfo] = {
    ("412000126173", "4"): CharacterInfo(
        uid="412000126173",
        server="4",
        role_name="Sweettoast",
        level=80,
        openid="MOCK-openid-sweettoast",
        avatar_url=None,
        is_mock=True,
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
        try:
            body = r.json()
        except ValueError:
            raise UpstreamUnavailableError("Partner returned invalid JSON.") from None
        if not isinstance(body, dict):
            raise UpstreamUnavailableError("Partner returned an invalid envelope.")
        # This is a configurable integration contract, not a discovered public API.
        if body.get("code") not in (None, 0, 200):
            raise UpstreamUnavailableError("Partner rejected the lookup.")
        d = body.get("data", body)
        if not isinstance(d, dict):
            raise UpstreamUnavailableError("Partner returned invalid role data.")
        returned_uid = d.get("uid", d.get("roleId"))
        returned_server = d.get("server", d.get("serverArea"))
        if (returned_uid is not None and str(returned_uid) != uid) or (returned_server is not None and str(returned_server) != server):
            raise UpstreamUnavailableError("Partner returned a different UID/server.")
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
        if not settings.account_bearer_token and not settings.account_session_cookie:
            raise RuntimeError("ANIIMO_ACCOUNT_BEARER_TOKEN is required for role_provider=session")

    async def resolve(self, uid: str, server: str) -> CharacterInfo:
        async with AniimoClient(self.s) as client:
            data = await client.player_roles(self.s.account_session_cookie)
        roles = data.get("roles") or []
        for role in roles:
            if not isinstance(role, dict):
                raise UpstreamUnavailableError("Invalid account role record.")
            if str(role.get("roleId")) == uid and str(role.get("serverArea")) == server:
                return CharacterInfo(
                    uid=uid,
                    server=server,
                    role_name=role.get("roleName") or role.get("nickname") or "",
                    level=role.get("level"),
                    openid=role.get("openId") or role.get("openid"),
                    avatar_url=role.get("avatarUrl") or role.get("avatar"),
                )
        raise UidNotInAccountError(uid, server)


def build_provider(settings: Settings) -> RoleProvider:
    match settings.role_provider:
        case "partner":
            return PartnerRoleProvider(settings)
        case "session":
            return SessionRoleProvider(settings)
        case _:
            return MockRoleProvider()
