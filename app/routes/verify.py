"""POST /api/v1/aniimo/verify — resolve a character from UID + server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.aniimo.providers import RoleProvider
from app.deps import client_key, get_limiter, get_provider, get_read_breaker
from app.routes.order import _upstream
from app.ratelimit import CircuitBreaker
from app.models import VerifyRequest, VerifyResponse
from app.ratelimit import TokenBucketLimiter
from app.validation import normalize_server, normalize_uid

router = APIRouter(prefix="/api/v1/aniimo", tags=["verify"])

_MOCK_NOTE = (
    "Illustrative mock result; not a live game lookup or proof of arbitrary-UID support."
)


@router.post("/verify", response_model=VerifyResponse)
async def verify(
    body: VerifyRequest,
    request: Request,
    provider: RoleProvider = Depends(get_provider),
    limiter: TokenBucketLimiter = Depends(get_limiter),
    breaker: CircuitBreaker = Depends(get_read_breaker),
) -> VerifyResponse:
    limiter.check(f"verify:{client_key(request)}")
    uid = normalize_uid(body.uid)
    server = normalize_server(body.server)
    character = await _upstream(breaker, lambda: provider.resolve(uid, server))
    return VerifyResponse(
        verified=True,
        character=character,
        source=provider.name,
        note=_MOCK_NOTE if character.is_mock else (
            "Verified only among the operator account's roles; arbitrary UID lookup is not available through this provider."
            if provider.name == "session" else None
        ),
    )
