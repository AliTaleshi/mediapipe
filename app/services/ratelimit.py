"""Fixed-window rate limiter backed by Redis, keyed per API key."""

import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

log = get_logger(__name__)
WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }


async def check_rate_limit(
    redis: Redis, identity: str, limit: int, *, now: float | None = None
) -> RateLimitResult:
    """Counts one request for ``identity``. Fails open if Redis is unreachable."""
    now = time.time() if now is None else now
    window = int(now // WINDOW_SECONDS)
    reset = int((window + 1) * WINDOW_SECONDS - now) or 1
    key = f"ratelimit:{identity}:{window}"
    try:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_SECONDS * 2)
        count, _ = await pipe.execute()
    except RedisError:
        log.warning("rate limiter unavailable; allowing request", exc_info=True)
        return RateLimitResult(True, limit, limit, reset)
    return RateLimitResult(count <= limit, limit, max(limit - count, 0), reset)
