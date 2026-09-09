"""SQS Consumer Background Worker with Priority Queue Tiers & Circuit Breaker Backpressure."""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

from app.config import get_settings
from app.services.job_service import job_service
from app.services.sqs_producer import _memory_queues
from app.services.llm_gateway import (
    gateway,
    LLMRequest,
    LLMMessage,
    CircuitState,
    CircuitBreakerOpenException,
    LLMGatewayException,
)
from app.services.cache_service import cache_service
from app.services.metrics_service import metrics_service

logger = logging.getLogger("chat_platform.sqs_consumer")
settings = get_settings()


class SQSConsumerWorker:
    """Background worker consuming SQS priority queues with Circuit Breaker backpressure control."""

    def __init__(self, poll_interval_seconds: float = 0.1):
        self.poll_interval = poll_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session = None
        self.probe_interval_seconds: float = 2.0
        self._last_probe_time: float = 0.0

    async def start(self) -> None:
        """Start the background consumer loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info("SQS Consumer Worker started successfully.")

    async def stop(self) -> None:
        """Stop the background consumer worker loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SQS Consumer Worker stopped.")

    async def _consume_loop(self) -> None:
        """Main event loop checking priority queues and honoring circuit breaker backpressure."""
        while self._running:
            try:
                processed = await self.process_next_job()
                if not processed:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Error in worker consumption loop: %s", exc)
                await asyncio.sleep(0.5)

    async def process_next_job(self) -> bool:
        """Attempt to fetch and process a job from priority queues in order (high -> standard -> batch)."""
        cb_state = gateway.primary_circuit.state

        # 1. Circuit Breaker OPEN: Apply Queue Backpressure
        if cb_state == CircuitState.OPEN:
            can_run, remaining = await gateway.primary_circuit.can_execute()
            if not can_run:
                # When Circuit Breaker is OPEN: fail-fast stale/queued items or pause polling
                # Check if there are queued items in local memory queues that should be fast-failed
                for tier_key in ("high", "standard", "batch"):
                    queue = _memory_queues[tier_key]
                    if not queue.empty():
                        try:
                            job_data = queue.get_nowait()
                            queue.task_done()
                            job_id = job_data["job_id"]
                            logger.warning(
                                "Circuit Breaker OPEN: Fast-failing queued job %s due to upstream backpressure",
                                job_id,
                            )
                            await job_service.update_job(
                                job_id=job_id,
                                status="failed",
                                error=f"Circuit Breaker for provider is OPEN. Request fast-failed due to backpressure. Retry in {remaining:.1f}s",
                            )
                            return True
                        except asyncio.QueueEmpty:
                            pass
                # Pause consumption to prevent pulling stale SQS messages
                await asyncio.sleep(0.5)
                return False

        # 2. Circuit Breaker HALF_OPEN: Permit low-rate probe messages
        if cb_state == CircuitState.HALF_OPEN:
            now = time.monotonic()
            if now - self._last_probe_time < self.probe_interval_seconds:
                # Throttle processing to allow probe request evaluation
                await asyncio.sleep(0.2)
                return False
            self._last_probe_time = now
            logger.info("Circuit Breaker is HALF_OPEN: Permitting 1 probe job from priority queue")

        # 3. Pull next job from priority queues: High > Standard > Batch
        job_data = await self._fetch_job_from_queues()
        if not job_data:
            return False

        # 4. Execute the fetched job
        await self._execute_job(job_data)
        return True

    async def _fetch_job_from_queues(self) -> Optional[Dict[str, Any]]:
        """Fetch job from local memory queues or SQS by priority: high -> standard -> batch."""
        # 1. Check in-memory queues first (used in local development and tests)
        for tier_key in ("high", "standard", "batch"):
            queue = _memory_queues[tier_key]
            if not queue.empty():
                try:
                    job_data = queue.get_nowait()
                    queue.task_done()
                    return job_data
                except asyncio.QueueEmpty:
                    pass

        # 2. Check AWS SQS if enabled
        if settings.SQS_ENABLED:
            try:
                import aioboto3
                if self._session is None:
                    self._session = aioboto3.Session()

                kwargs = {"region_name": settings.AWS_REGION}
                if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                    kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                    kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
                if settings.AWS_ENDPOINT_URL:
                    kwargs["endpoint_url"] = settings.AWS_ENDPOINT_URL

                queues_in_priority = [
                    (settings.SQS_HIGH_PRIORITY_URL, "high"),
                    (settings.SQS_STANDARD_URL, "standard"),
                    (settings.SQS_BATCH_URL, "batch"),
                ]

                async with self._session.client("sqs", **kwargs) as sqs:
                    for q_url, tier in queues_in_priority:
                        res = await sqs.receive_message(
                            QueueUrl=q_url,
                            MaxNumberOfMessages=1,
                            WaitTimeSeconds=1,
                        )
                        messages = res.get("Messages", [])
                        if messages:
                            msg = messages[0]
                            receipt_handle = msg["ReceiptHandle"]
                            job_data = json.loads(msg["Body"])
                            # Delete message from SQS upon successful retrieval
                            await sqs.delete_message(QueueUrl=q_url, ReceiptHandle=receipt_handle)
                            return job_data
            except Exception as exc:
                logger.warning("AWS SQS receive failed in worker: %s", exc)

        return None

    async def _execute_job(self, job_data: Dict[str, Any]) -> None:
        """Process job execution using LLM Gateway and update job status in Redis."""
        job_id = job_data["job_id"]
        logger.info("Worker starting processing for job %s", job_id)
        start_time = time.perf_counter()

        # Update status to 'processing'
        await job_service.update_job(job_id=job_id, status="processing")

        messages = [
            LLMMessage(role=m["role"], content=m["content"])
            for m in job_data.get("messages", [])
        ]
        target_model = job_data.get("model") or settings.PRIMARY_MODEL
        question_text = job_data.get("question", "")

        # 1. Cache Check if enabled
        if job_data.get("use_cache", True):
            cached = await cache_service.get(question_text, target_model)
            if cached:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                result = {
                    "answer": cached["content"],
                    "model_used": cached["model_used"],
                    "provider": cached["provider"],
                    "is_fallback": False,
                    "cache_hit": True,
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "latency_ms": round(latency_ms, 2),
                    },
                    "request_id": job_id,
                }
                await job_service.update_job(job_id=job_id, status="completed", result=result)
                logger.info("Job %s completed via cache hit", job_id)
                return

        # 2. Call Resilient LLM Gateway
        llm_request = LLMRequest(
            messages=messages,
            model=target_model,
            temperature=job_data.get("temperature", 0.7),
            max_tokens=job_data.get("max_tokens", 1024),
        )

        try:
            gateway_response = await gateway.generate(llm_request)
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            cost_usd = settings.calculate_cost(
                gateway_response.model_used,
                gateway_response.prompt_tokens,
                gateway_response.completion_tokens,
            )

            # Update cache
            if job_data.get("use_cache", True):
                await cache_service.set(question_text, target_model, gateway_response)

            result = {
                "answer": gateway_response.content,
                "model_used": gateway_response.model_used,
                "provider": gateway_response.provider,
                "is_fallback": gateway_response.is_fallback,
                "cache_hit": False,
                "usage": {
                    "prompt_tokens": gateway_response.prompt_tokens,
                    "completion_tokens": gateway_response.completion_tokens,
                    "total_tokens": gateway_response.total_tokens,
                    "estimated_cost_usd": cost_usd,
                    "latency_ms": round(latency_ms, 2),
                },
                "request_id": job_id,
            }

            await job_service.update_job(job_id=job_id, status="completed", result=result)
            logger.info("Job %s completed successfully in %.2fms", job_id, latency_ms)

        except CircuitBreakerOpenException as exc:
            logger.warning("Job %s fast-failed due to Circuit Breaker: %s", job_id, exc)
            await job_service.update_job(
                job_id=job_id,
                status="failed",
                error=str(exc),
            )
        except Exception as exc:
            logger.exception("Job %s failed with exception: %s", job_id, exc)
            await job_service.update_job(
                job_id=job_id,
                status="failed",
                error=str(exc),
            )


# Global consumer worker singleton
worker = SQSConsumerWorker()
