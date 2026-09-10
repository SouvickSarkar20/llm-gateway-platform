"""Chat API endpoint: wires authentication, rate limiting, caching, LLM gateway, DB usage tracking, and Idempotency."""

import logging
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.database import get_db
from app.models.user import User
from app.models.usage import LLMUsageLog, RequestStatus
from app.schemas.chat import ChatRequest, ChatResponse, UsageStats, AsyncJobResponse
from app.api.deps import require_user_or_admin, get_rate_limiter_guard, get_idempotency_key
from app.services.cache_service import cache_service
from app.services.rate_limiter import RateLimitResult
from app.services.job_service import job_service
from app.services.sqs_producer import sqs_producer
from app.services.idempotency_service import idempotency_service
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
):
    """Execute AI Question-Answering with caching, resilience, tenant billing, and idempotency."""
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

    # Resolve Tenant Identity
    tenant_id = (
        request.headers.get("X-Tenant-ID")
        or request.headers.get("X-API-Key")
        or getattr(current_user, "tenant_id", current_user.id)
    )

    # Attach rate limit headers if available
    rate_limit_result: RateLimitResult | None = getattr(request.state, "rate_limit_result", None)
    if rate_limit_result:
        response.headers["X-RateLimit-Limit"] = str(rate_limit_result.limit)
        response.headers["X-RateLimit-Remaining"] = str(rate_limit_result.remaining)
        response.headers["X-RateLimit-Reset"] = str(rate_limit_result.reset_in_seconds)

    # 2. Check Idempotency-Key Header
    idempotency_key = get_idempotency_key(request)
    if idempotency_key:
        status_state, cached_payload = await idempotency_service.get_or_lock(
            identifier=tenant_id, idempotency_key=idempotency_key
        )
        if status_state == "COMPLETED" and cached_payload:
            logger.info(
                "Idempotency hit for key '%s': returning cached response",
                idempotency_key,
            )
            response.headers["X-Idempotent-Replay"] = "true"
            return ChatResponse(**cached_payload)
        elif status_state == "PROCESSING":
            logger.warning(
                "Idempotency lock active for key '%s': returning HTTP 409 Conflict",
                idempotency_key,
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Request with Idempotency-Key '{idempotency_key}' is currently processing.",
            )

    # 3. Check Async Queue Offload
    is_async = payload.async_mode or (request.headers.get("X-Async-Request", "").lower() == "true")
    remaining_tokens = rate_limit_result.remaining if rate_limit_result else float("inf")
    if is_async or (remaining_tokens <= settings.ASYNC_BACKPRESSURE_THRESHOLD_REMAINING):
        logger.info(
            "Offloading request %s to SQS priority queue (async=%s, remaining_tokens=%.1f)",
            request_id,
            is_async,
            remaining_tokens,
        )
        tier = getattr(current_user, "tier", "default")
        job_data = await job_service.create_job(
            tenant_id=tenant_id,
            tier=tier,
            question=question_text,
            model=target_model,
            messages=messages,
            use_cache=payload.use_cache,
            temperature=payload.temperature or 0.7,
            max_tokens=payload.max_tokens or 1024,
        )
        await sqs_producer.enqueue_job(job_data)

        job_id = job_data["job_id"]
        poll_url = f"{settings.API_V1_PREFIX}/jobs/{job_id}"
        stream_url = f"{settings.API_V1_PREFIX}/jobs/{job_id}/stream"

        async_response = AsyncJobResponse(
            job_id=job_id,
            status="queued",
            message="LLM capacity saturated or async mode requested. Job placed on priority queue.",
            poll_url=poll_url,
            stream_url=stream_url,
        )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=async_response.model_dump(),
            headers={
                "Location": poll_url,
                "Retry-After": "2",
                "X-Job-ID": job_id,
            },
        )

    # 4. Redis Cache-Aside Check
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
                tenant_id=tenant_id,
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

            chat_resp = ChatResponse(
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

            if idempotency_key:
                await idempotency_service.save_completed(
                    identifier=tenant_id,
                    idempotency_key=idempotency_key,
                    response_data=chat_resp.model_dump(),
                )

            return chat_resp

    # Cache miss
    metrics_service.record_cache_miss()
    response.headers["X-Cache"] = "MISS"

    # 5. Call Resilient LLM Gateway
    llm_request = LLMRequest(
        messages=messages,
        model=target_model,
        temperature=payload.temperature or 0.7,
        max_tokens=payload.max_tokens or 1024,
    )

    try:
        gateway_response = await gateway.generate(llm_request)
    except CircuitBreakerOpenException as exc:
        if idempotency_key:
            await idempotency_service.release_lock(tenant_id, idempotency_key)
        logger.warning("Circuit breaker fast-fail for request %s: %s", request_id, exc)
        response.headers["Retry-After"] = str(int(exc.recovery_time_remaining))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            headers={"Retry-After": str(int(exc.recovery_time_remaining))},
        )
    except (LLMClientException, LLMGatewayException, Exception) as exc:
        if idempotency_key:
            await idempotency_service.release_lock(tenant_id, idempotency_key)
        if isinstance(exc, (LLMClientException, LLMGatewayException)):
            raise HTTPException(status_code=exc.status_code, detail=exc.message)
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

    # 6. Asynchronously Persist Usage & Cost in PostgreSQL with Tenant ID
    usage_status = (
        RequestStatus.FALLBACK if gateway_response.is_fallback else RequestStatus.SUCCESS
    )
    usage_log = LLMUsageLog(
        request_id=request_id,
        user_id=current_user.id,
        tenant_id=tenant_id,
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

    # 7. Populate Redis Cache
    if payload.use_cache:
        await cache_service.set(question_text, target_model, gateway_response)

    # 8. Record Prometheus Metrics
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

    chat_resp = ChatResponse(
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

    if idempotency_key:
        await idempotency_service.save_completed(
            identifier=tenant_id,
            idempotency_key=idempotency_key,
            response_data=chat_resp.model_dump(),
        )

    return chat_resp
