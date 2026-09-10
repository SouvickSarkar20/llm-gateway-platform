"""Idempotency Service backed by Redis for duplicate request prevention & replay."""

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple
from app.core.redis import get_redis_client

logger = logging.getLogger("chat_platform.idempotency")


class IdempotencyService:
    """Redis-backed Idempotency Lock & Result Caching Service."""

    def __init__(self, default_ttl_seconds: int = 86400):
        self.default_ttl = default_ttl_seconds
        self._in_memory_store: Dict[str, Tuple[str, Optional[dict], float]] = {}

    def _get_redis_key(self, identifier: str, idempotency_key: str) -> str:
        return f"idempotency:{identifier}:{idempotency_key}"

    async def get_or_lock(
        self,
        identifier: str,
        idempotency_key: str,
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Check or acquire an idempotency lock.

        Returns:
            ("COMPLETED", response_payload) - If already executed, return cached response payload.
            ("PROCESSING", None) - If request is currently executing in another thread.
            ("NEW", None) - If new request, successfully acquired PROCESSING lock.
        """
        ttl = ttl_seconds or self.default_ttl
        redis_key = self._get_redis_key(identifier, idempotency_key)
        now = time.time()

        try:
            client = await get_redis_client()

            # Attempt to set key to PROCESSING if not exists (NX)
            init_data = json.dumps({"status": "PROCESSING", "created_at": now})
            acquired = await client.set(redis_key, init_data, ex=ttl, nx=True)

            if acquired:
                logger.info(
                    "Acquired NEW idempotency lock for key '%s' (identifier='%s')",
                    idempotency_key,
                    identifier,
                )
                return "NEW", None

            # Key already exists: fetch current status & payload
            existing_val = await client.get(redis_key)
            if existing_val:
                record = json.loads(existing_val)
                status = record.get("status", "PROCESSING")
                if status == "COMPLETED":
                    logger.info(
                        "Found COMPLETED idempotent result for key '%s'",
                        idempotency_key,
                    )
                    return "COMPLETED", record.get("payload")
                else:
                    logger.warning(
                        "Idempotency key '%s' currently PROCESSING",
                        idempotency_key,
                    )
                    return "PROCESSING", None

            return "NEW", None

        except Exception as exc:
            logger.warning(
                "Redis Idempotency Exception (%s). Falling back to in-memory store.",
                exc,
            )
            # In-memory fallback
            if redis_key in self._in_memory_store:
                status, payload, expires_at = self._in_memory_store[redis_key]
                if now < expires_at:
                    if status == "COMPLETED":
                        return "COMPLETED", payload
                    else:
                        return "PROCESSING", None

            # Set as new in memory
            self._in_memory_store[redis_key] = ("PROCESSING", None, now + ttl)
            return "NEW", None

    async def save_completed(
        self,
        identifier: str,
        idempotency_key: str,
        response_data: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """Store completed execution result for future idempotent replay."""
        ttl = ttl_seconds or self.default_ttl
        redis_key = self._get_redis_key(identifier, idempotency_key)
        now = time.time()

        payload_data = json.dumps({
            "status": "COMPLETED",
            "payload": response_data,
            "created_at": now,
        })

        try:
            client = await get_redis_client()
            await client.set(redis_key, payload_data, ex=ttl)
            logger.info("Saved COMPLETED idempotent result for key '%s'", idempotency_key)
            return True
        except Exception as exc:
            logger.warning(
                "Failed to save idempotent response in Redis (%s). Saving to in-memory fallback.",
                exc,
            )
            self._in_memory_store[redis_key] = ("COMPLETED", response_data, now + ttl)
            return True

    async def release_lock(self, identifier: str, idempotency_key: str) -> None:
        """Release lock if execution failed before completion."""
        redis_key = self._get_redis_key(identifier, idempotency_key)
        try:
            client = await get_redis_client()
            await client.delete(redis_key)
        except Exception:
            pass
        self._in_memory_store.pop(redis_key, None)


# Singleton instance
idempotency_service = IdempotencyService()
