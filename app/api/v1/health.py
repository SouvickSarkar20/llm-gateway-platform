"""Health check endpoint performing deep dependency verification."""

from datetime import datetime
import logging
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.database import get_db
from app.core.redis import check_redis_health

logger = logging.getLogger("chat_platform.health")

router = APIRouter(prefix="/health", tags=["Health & Probes"])


@router.get(
    "",
    summary="Deep system health check verifying DB and Redis dependencies",
    status_code=status.HTTP_200_OK,
)
async def health_check(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Probe PostgreSQL database and Redis cluster to ensure liveness and readiness."""
    db_ok = False
    redis_ok = False
    details = []

    # 1. Probe Database
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.error("Database health probe failed: %s", exc)
        details.append(f"Database: {str(exc)}")

    # 2. Probe Redis
    try:
        redis_ok = await check_redis_health()
        if not redis_ok:
            details.append("Redis: Ping failed")
    except Exception as exc:
        logger.error("Redis health probe failed: %s", exc)
        details.append(f"Redis: {str(exc)}")

    all_healthy = db_ok and redis_ok
    if not all_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "healthy" if all_healthy else "unhealthy",
        "database": "connected" if db_ok else "disconnected",
        "redis": "connected" if redis_ok else "disconnected",
        # pyrefly: ignore [deprecated]
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": "1.0.0",
        "issues": details if not all_healthy else [],
    }
