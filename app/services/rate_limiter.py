"""Distributed sliding-window rate limiter powered by Redis."""

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple
from app.config import get_settings
from app.core.redis import get_redis_client

logger = logging.getLogger("chat_platform.rate_limiter")
settings = get_settings()


@dataclass
class RateLimitResult:
    """Outcome of rate limit evaluation."""
    allowed: bool
    limit: int
    remaining: int
    reset_in_seconds: int
    identifier: str


class DistributedRateLimiter:
    """Atomic sliding-window rate limiter using Redis sorted sets (ZSET)."""

    def __init__(
        self,
        default_requests_per_minute: Optional[int] = None,
        default_window_seconds: int = 60,
    ):
        self.default_limit = (
            default_requests_per_minute or settings.RATE_LIMIT_REQUESTS_PER_MINUTE
        )
        self.window_seconds = default_window_seconds

    async def check_rate_limit(
        self,
        identifier: str,
        max_requests: Optional[int] = None,
        window_seconds: Optional[int] = None,
    ) -> RateLimitResult:
        """Evaluate whether an action is within allowed sliding-window quota."""
        limit = max_requests or self.default_limit
        window = window_seconds or self.window_seconds
        key = f"ratelimit:{identifier}:{window}"

        now = time.time()
        window_start = now - window
        member_id = f"{now}:{uuid.uuid4().hex[:8]}"

        try:
            client = await get_redis_client()
            pipe = client.pipeline(transaction=True)

            # 1. Remove expired timestamps outside the rolling window
            pipe.zremrangebyscore(key, "-inf", window_start)
            # 2. Count current active requests in window
            pipe.zcard(key)
            # 3. Add current request timestamp
            pipe.zadd(key, {member_id: now})
            # 4. Set key TTL slightly longer than window to prevent orphan keys
            pipe.expire(key, window + 5)

            results = await pipe.execute()
            current_count = results[1]  # zcard output

            if current_count >= limit:
                # Quota exceeded - remove the newly added timestamp
                await client.zrem(key, member_id)
                reset_seconds = int(window)
                logger.warning(
                    "Rate limit EXCEEDED for identifier '%s' (%d/%d req per %ds)",
                    identifier,
                    current_count,
                    limit,
                    window,
                )
                return RateLimitResult(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_in_seconds=reset_seconds,
                    identifier=identifier,
                )

            remaining = max(0, limit - current_count - 1)
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=remaining,
                reset_in_seconds=int(window),
                identifier=identifier,
            )

        except Exception as exc:
            logger.error(
                "Redis rate limiter exception (%s). Failing open for availability.",
                exc,
            )
            # Graceful degradation: fail open so unexpected cache downtime doesn't block users
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=1,
                reset_in_seconds=int(window),
                identifier=identifier,
            )


# Global rate limiter instance
rate_limiter = DistributedRateLimiter()
