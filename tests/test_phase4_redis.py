"""Unit tests for Phase 4: Redis response caching and distributed rate limiting."""

import pytest
from app.services.cache_service import LLMCacheService
from app.services.llm_gateway import LLMResponse
from app.services.rate_limiter import DistributedRateLimiter


@pytest.mark.asyncio
async def test_cache_service_miss_and_hit():
    """Verify cache-aside behavior: miss on first call, hit on subsequent calls."""
    cache = LLMCacheService(default_ttl_seconds=60)
    question = "Explain CAP theorem in distributed systems"
    model = "gpt-4o-mini"

    # 1. First lookup should be a cache miss
    miss_result = await cache.get(question, model)
    assert miss_result is None

    # 2. Store mock response
    mock_resp = LLMResponse(
        content="CAP stands for Consistency, Availability, Partition tolerance.",
        model_used=model,
        provider="mock",
        prompt_tokens=15,
        completion_tokens=10,
        total_tokens=25,
        latency_ms=120.0,
    )
    saved = await cache.set(question, model, mock_resp)
    assert saved is True

    # 3. Subsequent lookup should be a cache hit
    hit_result = await cache.get(question, model)
    assert hit_result is not None
    assert hit_result["content"] == mock_resp.content
    assert hit_result["total_tokens"] == 25
    assert hit_result["cached"] is True


@pytest.mark.asyncio
async def test_cache_key_normalization():
    """Verify that case differences and leading/trailing whitespace produce the exact same cache hit."""
    cache = LLMCacheService(default_ttl_seconds=60)
    base_q = "What is Kubernetes?"
    variant_q = "   what   is   KUBERNETES?   "
    model = "gpt-4o-mini"

    key1 = cache.generate_cache_key(base_q, model)
    key2 = cache.generate_cache_key(variant_q, model)
    assert key1 == key2

    # Set using base query
    resp = LLMResponse(
        content="Kubernetes is a container orchestration platform.",
        model_used=model,
        provider="mock",
        prompt_tokens=10,
        completion_tokens=10,
        total_tokens=20,
        latency_ms=80.0,
    )
    await cache.set(base_q, model, resp)

    # Lookup using noisy variant query -> should hit!
    hit = await cache.get(variant_q, model)
    assert hit is not None
    assert "container orchestration" in hit["content"]


@pytest.mark.asyncio
async def test_distributed_rate_limiter_throttling():
    """Verify sliding-window rate limiter allows quota and rejects requests over the limit."""
    limiter = DistributedRateLimiter()
    test_user_id = "test-user-rate-limit"
    max_reqs = 3
    window_sec = 10

    # 1. Request 1: Allowed (remaining 2)
    res1 = await limiter.check_rate_limit(test_user_id, max_requests=max_reqs, window_seconds=window_sec)
    assert res1.allowed is True
    assert res1.remaining == 2

    # 2. Request 2: Allowed (remaining 1)
    res2 = await limiter.check_rate_limit(test_user_id, max_requests=max_reqs, window_seconds=window_sec)
    assert res2.allowed is True
    assert res2.remaining == 1

    # 3. Request 3: Allowed (remaining 0)
    res3 = await limiter.check_rate_limit(test_user_id, max_requests=max_reqs, window_seconds=window_sec)
    assert res3.allowed is True
    assert res3.remaining == 0

    # 4. Request 4: Over quota -> REJECTED
    res4 = await limiter.check_rate_limit(test_user_id, max_requests=max_reqs, window_seconds=window_sec)
    assert res4.allowed is False
    assert res4.remaining == 0
    assert res4.reset_in_seconds > 0
