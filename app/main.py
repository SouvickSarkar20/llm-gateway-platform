"""FastAPI application entrypoint with lifecycle, middleware, and routers."""

from contextlib import asynccontextmanager
import logging
import time
import uuid
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import get_settings
from app.core.database import init_db, engine
from app.core.redis import get_redis_client, close_redis
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.jobs import router as jobs_router
from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router
from app.workers.sqs_consumer import worker

# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("chat_platform")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager handling startup initialization and graceful shutdown."""
    logger.info("Starting %s in %s environment...", settings.APP_NAME, settings.APP_ENV)

    # 1. Initialize DB schema and seed default users
    try:
        await init_db()
        logger.info("Database initialized and seeded.")
    except Exception as exc:
        logger.error("Failed to initialize database: %s", exc)

    # 2. Warm up Redis connection
    try:
        await get_redis_client()
    except Exception as exc:
        logger.warning("Redis initial connection warning: %s", exc)

    # 3. Start SQS Consumer Worker
    try:
        await worker.start()
    except Exception as exc:
        logger.warning("Worker startup warning: %s", exc)

    yield

    # Shutdown
    logger.info("Shutting down %s...", settings.APP_NAME)
    await worker.stop()
    await close_redis()
    await engine.dispose()
    logger.info("Cleanup completed. Goodbye.")


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="Production-Ready AI Question-Answering Platform & Resilient LLM Gateway",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id_and_timing(request: Request, call_next):
    """Attach unique request_id and compute processing latency."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id

    start_time = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled server exception: %s", exc)
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error", "request_id": request_id},
        )

    latency_ms = (time.perf_counter() - start_time) * 1000.0
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{latency_ms:.2f}"
    return response


# Instrument FastAPI app for Prometheus monitoring
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics", "/health"],
).instrument(app)


# Mount routers directly per assessment specification
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(chat_router, prefix=settings.API_V1_PREFIX)
app.include_router(jobs_router)
app.include_router(jobs_router, prefix=settings.API_V1_PREFIX)
app.include_router(health_router)
app.include_router(metrics_router)


@app.get("/", tags=["System"])
async def root():
    """Service metadata and health pointers."""
    return {
        "service": settings.APP_NAME,
        "status": "online",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "metrics": settings.PROMETHEUS_METRICS_PATH,
    }
