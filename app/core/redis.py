"""Redis connection pool management with graceful fallback for standalone testing."""

import logging
from typing import Optional
import redis.asyncio as aioredis
from app.config import get_settings

logger = logging.getLogger("chat_platform.redis")
settings = get_settings()

_redis_client: Optional[aioredis.Redis] = None
_is_fake_redis: bool = False


async def get_redis_client() -> aioredis.Redis:
    """Obtain or initialize the async Redis client singleton."""
    global _redis_client, _is_fake_redis

    if _redis_client is not None:
        return _redis_client

    try:
        client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_SECONDS,
        )
        # Verify connection
        await client.ping()
        _redis_client = client
        _is_fake_redis = False
        logger.info("Connected to live Redis at %s", settings.REDIS_URL)
        return _redis_client
    except Exception as exc:
        logger.warning(
            "Could not connect to live Redis (%s). Initializing FakeRedis in-memory fallback.",
            exc,
        )
        try:
            import fakeredis.aioredis as fake_aioredis
            _redis_client = fake_aioredis.FakeRedis(decode_responses=True)
            _is_fake_redis = True
            logger.info("FakeRedis in-memory fallback active.")
            return _redis_client
        except ImportError:
            logger.error("fakeredis is not installed; Redis operations will fail.")
            raise exc


async def close_redis() -> None:
    """Close the Redis client pool."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.close()
        _redis_client = None


async def check_redis_health() -> bool:
    """Check connectivity to Redis."""
    try:
        client = await get_redis_client()
        await client.ping()
        return True
    except Exception as exc:
        logger.error("Redis health check failed: %s", exc)
        return False
