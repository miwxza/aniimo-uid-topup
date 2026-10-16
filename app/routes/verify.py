"""POST /api/v1/aniimo/verify — resolve a character from UID + server."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.aniimo.providers import RoleProvider
from app.deps import client_key, get_limiter, get_provider
from app.models import VerifyRequest, VerifyResponse
from app.ratelimit import TokenBucketLimiter

router = APIRouter(prefix="/api/v1/aniimo", tags=["verify"])


@router.post("/verify", response_model=VerifyResponse)
async def verify(
    body: VerifyRequest,
    request: Request,
    provider: RoleProvider = Depends(get_provider),
    limiter: TokenBucketLimiter = Depends(get_limiter),
) -> VerifyResponse:
    limiter.check(f"verify:{client_key(request)}")
    character = await provider.resolve(body.uid, body.server)
    return VerifyResponse(verified=True, character=character, source=provider.name)
