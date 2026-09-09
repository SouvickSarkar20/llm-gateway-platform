"""Unit tests for Stage 1: Atomic Token Bucket Rate Limiter via Redis Lua script and Per-Tenant API Keys."""

import asyncio
import time
import pytest
from app.services.rate_limiter import DistributedRateLimiter
from app.config import get_settings

settings = get_settings()


@pytest.mark.asyncio
async def test_token_bucket_burst_and_exhaustion():
    """Verify Token Bucket allows burst capacity and rejects once bucket is empty."""
    limiter = DistributedRateLimiter()
    identifier = "test-tenant-burst"
    capacity = 3
    window_sec = 10  # refill_rate = 3 / 10 = 0.3 tokens/sec

    # 1. Consume token 1 -> Allowed (remaining 2)
    res1 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert res1.allowed is True
    assert res1.remaining == 2

    # 2. Consume token 2 -> Allowed (remaining 1)
    res2 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert res2.allowed is True
    assert res2.remaining == 1

    # 3. Consume token 3 -> Allowed (remaining 0)
    res3 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert res3.allowed is True
    assert res3.remaining == 0

    # 4. Consume token 4 -> Rejected (0 tokens left)
    res4 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert res4.allowed is False
    assert res4.remaining == 0
    assert res4.reset_in_seconds > 0


@pytest.mark.asyncio
async def test_token_bucket_refill_over_time():
    """Verify that elapsed time refills tokens in the bucket atomically."""
    limiter = DistributedRateLimiter()
    identifier = "test-tenant-refill"
    capacity = 2
    window_sec = 2  # refill_rate = 1.0 token/sec

    # Drain bucket (2 tokens)
    r1 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    r2 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert r1.allowed is True
    assert r2.allowed is True

    # Immediate third request should fail
    r3 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert r3.allowed is False

    # Wait 1.1 seconds -> 1 token should be refilled
    await asyncio.sleep(1.1)

    # Fourth request should now succeed
    r4 = await limiter.check_rate_limit(identifier, max_requests=capacity, window_seconds=window_sec)
    assert r4.allowed is True


@pytest.mark.asyncio
async def test_tenant_tier_quotas():
    """Verify free vs enterprise tier capacity and rate limit resolution."""
    limiter = DistributedRateLimiter()

    # Free tier test
    free_res = await limiter.check_rate_limit("tenant-free-1", tenant_tier="free")
    assert free_res.limit == int(settings.TENANT_TIERS["free"]["capacity"])

    # Enterprise tier test
    ent_res = await limiter.check_rate_limit("tenant-ent-1", tenant_tier="enterprise")
    assert ent_res.limit == int(settings.TENANT_TIERS["enterprise"]["capacity"])
