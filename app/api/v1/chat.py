"""Chat API endpoint: wires authentication, rate limiting, caching, LLM gateway, and DB usage tracking."""

import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.database import get_db
from app.models.user import User
from app.models.usage import LLMUsageLog, RequestStatus
from app.schemas.chat import ChatRequest, ChatResponse, UsageStats
from app.api.deps import require_user_or_admin, get_rate_limiter_guard
from app.services.cache_service import cache_service
from app.services.rate_limiter import RateLimitResult
from app.services.llm_gateway import (
    gateway,
    LLMRequest,
    LLMMessage,
    LLMClientException,
    CircuitBreakerOpenException,
    LLMGatewayException,
)
from app.services.metrics_service import metrics_service

logger = logging.getLogger("chat_platform.chat")
settings = get_settings()

router = APIRouter(prefix="/chat", tags=["Chat & LLM Gateway"])


@router.post(
    "",
    response_model=ChatResponse,
    summary="Submit question to resilient LLM Gateway",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_rate_limiter_guard())],
)
async def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(require_user_or_admin),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Execute AI Question-Answering with caching, resilience, and token/cost auditing."""
    request_id = getattr(request.state, "request_id", "req-unknown")
    start_time = time.perf_counter()

    # 1. Resolve question text and message list
    if not payload.question and not payload.messages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'question' or 'messages' must be provided.",
        )

    question_text: str
    if payload.question and not payload.messages:
        question_text = payload.question
        messages = [LLMMessage(role="user", content=question_text)]
    elif payload.messages and not payload.question:
        messages = [LLMMessage(role=m.role, content=m.content) for m in payload.messages]
        question_text = payload.messages[-1].content
    else:
        question_text = payload.question or (payload.messages[-1].content if payload.messages else "")
        messages = [LLMMessage(role=m.role, content=m.content) for m in (payload.messages or [])]

    target_model = payload.model or settings.PRIMARY_MODEL

    # Attach rate limit headers if available
    rate_limit_result: RateLimitResult | None = getattr(request.state, "rate_limit_result", None)
    if rate_limit_result:
        response.headers["X-RateLimit-Limit"] = str(rate_limit_result.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_limit_result.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_limit_result.reset_in_seconds)

    # 2. Redis Cache-Aside Check
    if payload.use_cache:
        cached_result = await cache_service.get(question_text, target_model)
        if cached_result:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            response.headers["X-Cache"] = "HIT"
            response.headers["X-Model-Used"] = cached_result["model_used"]
            metrics_service.record_cache_hit()

            # Record 0-cost usage in database
            usage_log = LLMUsageLog(
                request_id=request_id,
                user_id=current_user.id,
                model=cached_result["model_used"],
                provider=cached_result["provider"],
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=round(latency_ms, 2),
                cache_hit=True,
                is_fallback=False,
                status=RequestStatus.CACHE_HIT,
            )
            db.add(usage_log)
            await db.commit()

            return ChatResponse(
                answer=cached_result["content"],
                model_used=cached_result["model_used"],
                provider=cached_result["provider"],
                is_fallback=False,
                cache_hit=True,
                usage=UsageStats(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    estimated_cost_usd=0.0,
                    latency_ms=round(latency_ms, 2),
                ),
                request_id=request_id,
            )

    # Cache miss
    metrics_service.record_cache_miss()
    response.headers["X-Cache"] = "MISS"

    # 3. Call Resilient LLM Gateway
    llm_request = LLMRequest(
        messages=messages,
        model=target_model,
        temperature=payload.temperature or 0.7,
        max_tokens=payload.max_tokens or 1024,
    )

    try:
        gateway_response = await gateway.generate(llm_request)
    except CircuitBreakerOpenException as exc:
        logger.warning("Circuit breaker fast-fail for request %s: %s", request_id, exc)
        response.headers["Retry-After"] = str(int(exc.recovery_time_remaining))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.recovery_time_remaining))},
        )
    except LLMClientException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except LLMGatewayException as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    except Exception as exc:
        logger.exception("Unexpected error in LLM gateway execution: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal LLM gateway processing error",
        )

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    cost_usd = settings.calculate_cost(
        gateway_response.model_used,
        gateway_response.prompt_tokens,
        gateway_response.completion_tokens,
    )

    # 4. Asynchronously Persist Usage & Cost in PostgreSQL
    usage_status = (
        RequestStatus.FALLBACK if gateway_response.is_fallback else RequestStatus.SUCCESS
    )
    usage_log = LLMUsageLog(
        request_id=request_id,
        user_id=current_user.id,
        model=gateway_response.model_used,
        provider=gateway_response.provider,
        prompt_tokens=gateway_response.prompt_tokens,
        completion_tokens=gateway_response.completion_tokens,
        total_tokens=gateway_response.total_tokens,
        estimated_cost_usd=cost_usd,
        latency_ms=round(latency_ms, 2),
        cache_hit=False,
        is_fallback=gateway_response.is_fallback,
        status=usage_status,
    )
    db.add(usage_log)
    await db.commit()

    # 5. Populate Redis Cache
    if payload.use_cache:
        await cache_service.set(question_text, target_model, gateway_response)

    # 6. Record Prometheus Metrics
    metrics_service.record_llm_execution(
        model=gateway_response.model_used,
        provider=gateway_response.provider,
        status=usage_status.value,
        is_fallback=gateway_response.is_fallback,
        prompt_tokens=gateway_response.prompt_tokens,
        completion_tokens=gateway_response.completion_tokens,
        duration_seconds=latency_ms / 1000.0,
    )

    response.headers["X-Model-Used"] = gateway_response.model_used
    if gateway_response.is_fallback:
        response.headers["X-Fallback-Triggered"] = "true"

    return ChatResponse(
        answer=gateway_response.content,
        model_used=gateway_response.model_used,
        provider=gateway_response.provider,
        is_fallback=gateway_response.is_fallback,
        cache_hit=False,
        usage=UsageStats(
            prompt_tokens=gateway_response.prompt_tokens,
            completion_tokens=gateway_response.completion_tokens,
            total_tokens=gateway_response.total_tokens,
            estimated_cost_usd=cost_usd,
            latency_ms=round(latency_ms, 2),
        ),
        request_id=request_id,
    )
