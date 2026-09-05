"""Comprehensive unit tests for Phase 3: Resilient LLM Gateway & Resilience Mechanics."""

import asyncio
import pytest
from app.services.llm_gateway import (
    CircuitBreaker,
    CircuitState,
    CircuitBreakerOpenException,
    LLMClientException,
    LLMGatewayException,
    LLMMessage,
    LLMRequest,
    LLMServerException,
    LLMTimeoutException,
    MockLLMProvider,
    ResilientLLMGateway,
    is_retryable_error,
)
from app.config import get_settings

settings = get_settings()


@pytest.mark.asyncio
async def test_llm_gateway_happy_path():
    """Test standard successful generation through primary model."""
    gw = ResilientLLMGateway()
    req = LLMRequest(
        messages=[LLMMessage(role="user", content="Explain microservices architecture")]
    )
    resp = await gw.generate(req)

    assert resp.model_used == settings.PRIMARY_MODEL
    assert resp.is_fallback is False
    assert resp.total_tokens > 0
    assert "microservices architecture" in resp.content
    assert resp.latency_ms > 0


@pytest.mark.asyncio
async def test_retry_on_transient_failure_then_success():
    """Test that transient timeout triggers backoff retry and succeeds on attempt 2."""
    gw = ResilientLLMGateway()
    mock_provider = MockLLMProvider()
    # Inject 1 transient timeout failure
    mock_provider.set_fault("timeout", count=1)
    gw._providers["mock"] = mock_provider

    req = LLMRequest(messages=[LLMMessage(role="user", content="Transient failure test")])
    resp = await gw.generate(req)

    # Should have succeeded on primary model after 1 retry
    assert resp.model_used == settings.PRIMARY_MODEL
    assert resp.is_fallback is False
    assert mock_provider.fail_count == 0


@pytest.mark.asyncio
async def test_fallback_model_trigger_when_primary_fails():
    """Test that when primary model exhausts retries, it cleanly falls back to secondary model."""
    gw = ResilientLLMGateway()
    mock_provider = MockLLMProvider()
    # Inject 5 failures (more than MAX_RETRIES=3), so primary fails completely
    mock_provider.set_fault("server_error", count=5)
    gw._providers["mock"] = mock_provider

    req = LLMRequest(messages=[LLMMessage(role="user", content="Will primary failover?")])
    resp = await gw.generate(req)

    # Fallback model should have handled the request
    assert resp.is_fallback is True
    assert resp.model_used == settings.FALLBACK_MODEL
    assert "Will primary failover?" in resp.content


@pytest.mark.asyncio
async def test_non_retryable_client_error_fails_immediately():
    """Verify that HTTP 400 bad prompt does NOT retry and does NOT trigger fallback."""
    gw = ResilientLLMGateway()
    mock_provider = MockLLMProvider()
    mock_provider.set_fault("client_error", count=1)
    gw._providers["mock"] = mock_provider

    req = LLMRequest(messages=[LLMMessage(role="user", content="Invalid prompt")])

    with pytest.raises(LLMClientException) as exc_info:
        await gw.generate(req)

    assert exc_info.value.status_code == 400
    assert not is_retryable_error(exc_info.value)


@pytest.mark.asyncio
async def test_circuit_breaker_state_transitions():
    """Verify 3-state Circuit Breaker: CLOSED -> OPEN on 5 failures -> fast fail -> HALF_OPEN -> CLOSED."""
    cb = CircuitBreaker(
        name="test_circuit",
        failure_threshold=3,
        recovery_timeout=0.2,  # 200ms recovery for fast testing
        half_open_success_threshold=2,
    )

    assert cb.state == CircuitState.CLOSED
    can_exec, _ = await cb.can_execute()
    assert can_exec is True

    # 1. Cause 2 failures (under threshold) -> Still CLOSED
    await cb.record_failure()
    await cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    # 2. 3rd failure reaches threshold -> Trips to OPEN
    await cb.record_failure()
    assert cb.state == CircuitState.OPEN

    # In OPEN state, can_execute returns False
    can_exec, remaining = await cb.can_execute()
    assert can_exec is False
    assert remaining > 0

    # 3. Wait for recovery timeout
    await asyncio.sleep(0.25)

    # In HALF_OPEN state, probe request is allowed
    can_exec, _ = await cb.can_execute()
    assert can_exec is True
    assert cb.state == CircuitState.HALF_OPEN

    # 4. First success in HALF_OPEN -> still HALF_OPEN
    await cb.record_success()
    assert cb.state == CircuitState.HALF_OPEN

    # 5. Second success meets half_open_success_threshold -> transitions back to CLOSED
    await cb.record_success()
    assert cb.state == CircuitState.CLOSED
