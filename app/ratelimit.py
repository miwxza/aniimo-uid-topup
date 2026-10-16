"""In-process token-bucket rate limiter + upstream circuit breaker.

For a single-node deployment this is enough to protect both our callers and the
upstream Aniimo API. For a horizontally-scaled deployment, back the bucket with
Redis (same algorithm) — see docs/EDGE_CASES.md.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.errors import RateLimitedError, UpstreamUnavailableError


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, per_min: int, burst: int):
        self.rate = per_min / 60.0  # tokens per second
        self.burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str) -> None:
        now = time.monotonic()
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(tokens=self.burst, updated=now)
            self._buckets[key] = b
        # Refill.
        b.tokens = min(self.burst, b.tokens + (now - b.updated) * self.rate)
        b.updated = now
        if b.tokens < 1.0:
            retry = max(1, int((1.0 - b.tokens) / self.rate))
            raise RateLimitedError(retry_after=retry)
        b.tokens -= 1.0


@dataclass
class CircuitBreaker:
    """Opens after N consecutive failures; half-opens after a cooldown."""

    threshold: int
    cooldown: float
    _failures: int = 0
    _opened_at: float | None = field(default=None)

    def before(self) -> None:
        if self._opened_at is not None:
            if time.monotonic() - self._opened_at < self.cooldown:
                raise UpstreamUnavailableError("circuit open; upstream cooling down")
            # half-open: allow a trial request
            self._opened_at = None

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.threshold:
            self._opened_at = time.monotonic()
