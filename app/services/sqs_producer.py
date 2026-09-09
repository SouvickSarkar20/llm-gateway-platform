"""Async SQS Producer for routing LLM request jobs to priority FIFO queues."""

import asyncio
import json
import logging
from typing import Any, Dict, Optional
from app.config import get_settings

logger = logging.getLogger("chat_platform.sqs_producer")
settings = get_settings()

# In-memory queue fallback for offline development & automated tests
_memory_queues: Dict[str, asyncio.Queue] = {
    "high": asyncio.Queue(),
    "standard": asyncio.Queue(),
    "batch": asyncio.Queue(),
}


class SQSProducerService:
    """Publishes jobs to tier-specific SQS FIFO queues or local memory queue fallback."""

    def __init__(self):
        self._session = None

    def _get_queue_url_and_tier_key(self, tier: str) -> tuple[str, str]:
        normalized_tier = tier.lower()
        if normalized_tier == "enterprise":
            return settings.SQS_HIGH_PRIORITY_URL, "high"
        elif normalized_tier == "pro":
            return settings.SQS_STANDARD_URL, "standard"
        else:
            return settings.SQS_BATCH_URL, "batch"

    async def enqueue_job(self, job_data: Dict[str, Any]) -> bool:
        """Enqueue job to appropriate SQS FIFO queue or local fallback."""
        tier = job_data.get("tier", "default")
        job_id = job_data["job_id"]
        tenant_id = job_data.get("tenant_id", "default_tenant")
        queue_url, tier_key = self._get_queue_url_and_tier_key(tier)

        payload_str = json.dumps(job_data)

        # 1. AWS SQS Execution Path (if SQS_ENABLED is True)
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

                async with self._session.client("sqs", **kwargs) as sqs:
                    # FIFO Queues require MessageGroupId and MessageDeduplicationId
                    await sqs.send_message(
                        QueueUrl=queue_url,
                        MessageBody=payload_str,
                        MessageGroupId=f"tenant-{tenant_id}",
                        MessageDeduplicationId=job_id,
                    )
                logger.info("Enqueued job %s to SQS FIFO queue '%s' (tier=%s)", job_id, queue_url, tier)
                return True
            except Exception as exc:
                logger.warning("AWS SQS publish failed for job %s, falling back to local queue: %s", job_id, exc)

        # 2. Local Fallback Queue Path
        await _memory_queues[tier_key].put(job_data)
        logger.info("Enqueued job %s to in-memory fallback queue '%s' (tier=%s)", job_id, tier_key, tier)
        return True


# Global producer singleton
sqs_producer = SQSProducerService()
