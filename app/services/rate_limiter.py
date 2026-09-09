"""Distributed atomic Token Bucket rate limiter powered by Redis Lua script with in-memory fallback."""

import logging
import math
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from app.config import get_settings
from app.core.redis import get_redis_client

logger = logging.getLogger("chat_platform.rate_limiter")
settings = get_settings()

TOKEN_BUCKET_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

-- Retrieve current state from Hash key
local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last_refill = tonumber(data[2])

if not tokens or not last_refill then
    tokens = capacity
    last_refill = now
else
    local delta = math.max(0, now - last_refill)
    tokens = math.min(capacity, tokens + (delta * refill_rate))
    last_refill = now
end

local allowed = 0
local reset_in = 0

if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
    reset_in = 0
else
    allowed = 0
    local needed = requested - tokens
    reset_in = math.ceil(needed / refill_rate)
end

-- Store updated state with TTL
local ttl = math.max(60, math.ceil(capacity / math.max(refill_rate, 0.001)) + 10)
redis.call('HMSET', key, 'tokens', tokens, 'last_refill', last_refill)
redis.call('EXPIRE', key, ttl)

return { allowed, math.floor(tokens), reset_in }
"""


@dataclass
class RateLimitResult:
    """Outcome of token bucket rate limit evaluation."""
    allowed: bool
    limit: int
    remaining: int
    reset_in_seconds: int
    identifier: str


class DistributedRateLimiter:
    """Atomic Token Bucket rate limiter using Redis Lua script with local fallback."""

    def __init__(
        self,
        default_requests_per_minute: Optional[int] = None,
        default_window_seconds: int = 60,
    ):
        self.default_limit = (
            default_requests_per_minute or settings.RATE_LIMIT_REQUESTS_PER_MINUTE
        )
        self.window_seconds = default_window_seconds
        self._in_memory_buckets: Dict[str, Tuple[float, float]] = {}

    def _fallback_check(
        self,
        key: str,
        capacity: float,
        refill_rate: float,
        now: float,
        requested_tokens: int,
        identifier: str,
    ) -> RateLimitResult:
        """In-memory Token Bucket fallback for FakeRedis or local offline execution."""
        if key not in self._in_memory_buckets:
            tokens = capacity
            last_refill = now
        else:
            tokens, last_refill = self._in_memory_buckets[key]
            delta = max(0.0, now - last_refill)
            tokens = min(capacity, tokens + (delta * refill_rate))
            last_refill = now

        if tokens >= requested_tokens:
            tokens -= requested_tokens
            self._in_memory_buckets[key] = (tokens, last_refill)
            return RateLimitResult(
                allowed=True,
                limit=int(capacity),
                remaining=int(tokens),
                reset_in_seconds=0,
                identifier=identifier,
            )
        else:
            needed = requested_tokens - tokens
            reset_in = int(math.ceil(needed / max(refill_rate, 0.001)))
            self._in_memory_buckets[key] = (tokens, last_refill)
            return RateLimitResult(
                allowed=False,
                limit=int(capacity),
                remaining=0,
                reset_in_seconds=reset_in,
                identifier=identifier,
            )

    async def check_rate_limit(
        self,
        identifier: str,
        max_requests: Optional[int] = None,
        window_seconds: Optional[int] = None,
        tenant_tier: Optional[str] = None,
        requested_tokens: int = 1,
    ) -> RateLimitResult:
        """Evaluate token bucket quota atomically using Redis Lua script or fallback."""
        # 1. Resolve capacity and refill rate based on tenant tier or defaults
        tier_config = settings.TENANT_TIERS.get(
            tenant_tier or "default", settings.TENANT_TIERS["default"]
        )

        if max_requests is not None:
            limit = max_requests
            window = window_seconds or self.window_seconds
            capacity = float(limit)
            refill_rate = capacity / float(window)
        else:
            limit = int(tier_config["rpm"])
            capacity = float(tier_config["capacity"])
            refill_rate = float(tier_config["refill_rate"])

        key = f"ratelimit:tb:{identifier}"
        now = time.time()

        try:
            client = await get_redis_client()
            
            # Execute atomic Lua script
            # pyrefly: ignore [not-async]
            result = await client.eval(
                TOKEN_BUCKET_LUA_SCRIPT,
                1,
                key,
                str(capacity),
                str(refill_rate),
                str(now),
                str(requested_tokens),
            )

            is_allowed = bool(result[0] == 1)
            remaining_tokens = int(result[1])
            reset_in_seconds = int(result[2])

            if not is_allowed:
                logger.warning(
                    "Token Bucket Rate limit EXCEEDED for '%s' (capacity=%.1f, refill=%.2f/s, remaining=%d, reset_in=%ds)",
                    identifier,
                    capacity,
                    refill_rate,
                    remaining_tokens,
                    reset_in_seconds,
                )

            return RateLimitResult(
                allowed=is_allowed,
                limit=int(capacity),
                remaining=remaining_tokens,
                reset_in_seconds=reset_in_seconds,
                identifier=identifier,
            )

        except Exception as exc:
            logger.warning(
                "Redis Token Bucket eval exception (%s). Using in-memory fallback.",
                exc,
            )
            return self._fallback_check(
                key=key,
                capacity=capacity,
                refill_rate=refill_rate,
                now=now,
                requested_tokens=requested_tokens,
                identifier=identifier,
            )


# Global rate limiter instance
rate_limiter = DistributedRateLimiter()
