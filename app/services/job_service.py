"""Job service for tracking async LLM request statuses in Redis/Memory."""

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional
from app.config import get_settings
from app.core.redis import get_redis_client

logger = logging.getLogger("chat_platform.job_service")
settings = get_settings()


class JobService:
    """Service to create, fetch, and update background job execution states."""

    def __init__(self):
        # In-memory fallback dictionary when Redis is unavailable
        self._memory_jobs: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _make_key(job_id: str) -> str:
        return f"job:{job_id}"

    async def create_job(
        self,
        tenant_id: str,
        tier: str,
        question: str,
        model: str,
        messages: list,
        use_cache: bool = True,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        custom_job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new job record in queued state."""
        job_id = custom_job_id or f"job-{uuid.uuid4().hex[:12]}"
        now = time.time()
        job_data = {
            "job_id": job_id,
            "status": "queued",  # queued, processing, completed, failed
            "tenant_id": tenant_id,
            "tier": tier,
            "question": question,
            "model": model,
            "messages": [m if isinstance(m, dict) else m.to_dict() for m in messages],
            "use_cache": use_cache,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
        }

        key = self._make_key(job_id)
        ttl = settings.JOB_TTL_SECONDS
        self._memory_jobs[job_id] = job_data

        try:
            client = await get_redis_client()
            await client.set(key, json.dumps(job_data), ex=ttl)
            logger.info("Created job %s in Redis (tenant=%s, tier=%s)", job_id, tenant_id, tier)
        except Exception as exc:
            logger.warning("Redis store failed for job %s, using in-memory: %s", job_id, exc)

        return job_data

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve job details by job_id."""
        key = self._make_key(job_id)
        try:
            client = await get_redis_client()
            raw = await client.get(key)
            if raw:
                return json.loads(raw)
        except Exception as exc:
            logger.warning("Redis fetch failed for job %s: %s", job_id, exc)

        return self._memory_jobs.get(job_id)

    async def update_job(
        self,
        job_id: str,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update job status and results."""
        job_data = await self.get_job(job_id)
        if not job_data:
            logger.error("Attempted to update non-existent job %s", job_id)
            return None

        job_data["status"] = status
        job_data["updated_at"] = time.time()
        if result is not None:
            job_data["result"] = result
        if error is not None:
            job_data["error"] = error

        key = self._make_key(job_id)
        ttl = settings.JOB_TTL_SECONDS
        self._memory_jobs[job_id] = job_data

        try:
            client = await get_redis_client()
            await client.set(key, json.dumps(job_data), ex=ttl)
            logger.info("Updated job %s state to '%s'", job_id, status)
        except Exception as exc:
            logger.warning("Redis update failed for job %s: %s", job_id, exc)

        return job_data


# Global singleton instance
job_service = JobService()
