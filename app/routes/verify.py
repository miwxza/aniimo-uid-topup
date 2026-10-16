"""POST /api/v1/aniimo/verify — resolve a character from UID + server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.aniimo.providers import RoleProvider
from app.deps import client_key, get_limiter, get_provider
from app.models import VerifyRequest, VerifyResponse
from app.ratelimit import TokenBucketLimiter
from app.validation import normalize_server, normalize_uid

router = APIRouter(prefix="/api/v1/aniimo", tags=["verify"])

_MOCK_NOTE = (
    "Illustrative mock result — not a live game lookup. Arbitrary-UID verification "
    "requires a partner/session provider (see README)."
)


@router.post("/verify", response_model=VerifyResponse)
async def verify(
    body: VerifyRequest,
    request: Request,
    provider: RoleProvider = Depends(get_provider),
    limiter: TokenBucketLimiter = Depends(get_limiter),
) -> VerifyResponse:
    limiter.check(f"verify:{client_key(request)}")
    uid = normalize_uid(body.uid)
    server = normalize_server(body.server)
    character = await provider.resolve(uid, server)
    return VerifyResponse(
        verified=True,
        character=character,
        source=provider.name,
        note=_MOCK_NOTE if character.is_mock else None,
    )
