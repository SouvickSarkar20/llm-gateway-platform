"""Redis response caching service for LLM question-answering acceleration."""

import hashlib
import json
import logging
from typing import Any, Dict, Optional
from app.config import get_settings
from app.core.redis import get_redis_client
from app.services.llm_gateway import LLMResponse

logger = logging.getLogger("chat_platform.cache")
settings = get_settings()


class LLMCacheService:
    """Cache-aside implementation for identical/near-identical LLM queries."""

    def __init__(self, default_ttl_seconds: Optional[int] = None):
        self.default_ttl = default_ttl_seconds or settings.REDIS_CACHE_TTL_SECONDS

    @staticmethod
    def generate_cache_key(question: str, model: str, system_prompt: str = "") -> str:
        """Create deterministic SHA-256 fingerprint from query and configuration parameters."""
        normalized_q = " ".join(question.strip().lower().split())
        normalized_sys = " ".join(system_prompt.strip().lower().split())
        raw_key = f"model={model}:sys={normalized_sys}:q={normalized_q}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        return f"cache:llm:{digest}"

    async def get(self, question: str, model: str, system_prompt: str = "") -> Optional[Dict[str, Any]]:
        """Retrieve cached response if present in Redis."""
        key = self.generate_cache_key(question, model, system_prompt)
        try:
            client = await get_redis_client()
            cached_data = await client.get(key)
            if cached_data:
                logger.info("Redis cache HIT for key: %s (query: '%.30s...')", key, question)
                return json.loads(cached_data)
            logger.debug("Redis cache MISS for key: %s", key)
            return None
        except Exception as exc:
            logger.warning("Redis cache get operation failed: %s", exc)
            return None

    async def set(
        self,
        question: str,
        model: str,
        response: LLMResponse,
        system_prompt: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """Persist generated LLM response into Redis cache with TTL."""
        key = self.generate_cache_key(question, model, system_prompt)
        ttl = ttl_seconds or self.default_ttl
        payload = {
            "content": response.content,
            "model_used": response.model_used,
            "provider": response.provider,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
            "cached": True,
        }
        try:
            client = await get_redis_client()
            await client.set(key, json.dumps(payload), ex=ttl)
            logger.info("Persisted response in Redis cache (key: %s, TTL: %ds)", key, ttl)
            return True
        except Exception as exc:
            logger.warning("Redis cache set operation failed: %s", exc)
            return False


# Global cache service instance
cache_service = LLMCacheService()
