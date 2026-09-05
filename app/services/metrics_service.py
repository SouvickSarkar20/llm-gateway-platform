"""Prometheus instrumentation and real-time platform metrics service."""

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
    REGISTRY,
)
from fastapi import Response
from app.services.llm_gateway import CircuitState

# 1. HTTP Request Metrics
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the platform",
    ["method", "endpoint", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP end-to-end request latency in seconds",
    ["endpoint", "method"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# 2. LLM Gateway & Token Metrics
LLM_REQUESTS_TOTAL = Counter(
    "llm_gateway_requests_total",
    "Total requests processed by the LLM Gateway",
    ["model", "provider", "status", "is_fallback"],
)

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total prompt and completion tokens consumed",
    ["model", "token_type"],  # token_type: "prompt", "completion"
)

LLM_REQUEST_DURATION_SECONDS = Histogram(
    "llm_request_duration_seconds",
    "Latency of external LLM API invocations in seconds",
    ["model", "provider"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0],
)

# 3. Circuit Breaker Gauges (0=CLOSED, 1=HALF_OPEN, 2=OPEN)
CIRCUIT_BREAKER_STATE = Gauge(
    "llm_circuit_breaker_state",
    "Current circuit breaker state: 0=CLOSED, 1=HALF_OPEN, 2=OPEN",
    ["provider"],
)

# 4. Caching Metrics
CACHE_HITS_TOTAL = Counter(
    "llm_cache_hits_total",
    "Total queries served from Redis cache without LLM invocation",
)

CACHE_MISSES_TOTAL = Counter(
    "llm_cache_misses_total",
    "Total queries requiring fresh LLM generation",
)

# 5. Rate Limiting Metrics
RATE_LIMIT_EXCEEDED_TOTAL = Counter(
    "llm_rate_limit_exceeded_total",
    "Total requests rejected with HTTP 429 due to quota exhaustion",
    ["identifier_type"],  # "user" or "ip"
)


class MetricsService:
    """Helper service managing Prometheus metric updates."""

    @staticmethod
    def record_cache_hit():
        CACHE_HITS_TOTAL.inc()

    @staticmethod
    def record_cache_miss():
        CACHE_MISSES_TOTAL.inc()

    @staticmethod
    def record_rate_limit_blocked(identifier: str):
        id_type = "user" if identifier.startswith("user:") else "ip"
        RATE_LIMIT_EXCEEDED_TOTAL.labels(identifier_type=id_type).inc()

    @staticmethod
    def update_circuit_breaker_state(provider: str, state: CircuitState):
        state_map = {
            CircuitState.CLOSED: 0,
            CircuitState.HALF_OPEN: 1,
            CircuitState.OPEN: 2,
        }
        CIRCUIT_BREAKER_STATE.labels(provider=provider).set(state_map.get(state, 0))

    @staticmethod
    def record_llm_execution(
        model: str,
        provider: str,
        status: str,
        is_fallback: bool,
        prompt_tokens: int,
        completion_tokens: int,
        duration_seconds: float,
    ):
        LLM_REQUESTS_TOTAL.labels(
            model=model,
            provider=provider,
            status=status,
            is_fallback=str(is_fallback).lower(),
        ).inc()

        if prompt_tokens > 0:
            LLM_TOKENS_TOTAL.labels(model=model, token_type="prompt").inc(prompt_tokens)
        if completion_tokens > 0:
            LLM_TOKENS_TOTAL.labels(model=model, token_type="completion").inc(completion_tokens)

        LLM_REQUEST_DURATION_SECONDS.labels(model=model, provider=provider).observe(duration_seconds)

    @staticmethod
    def export_metrics() -> Response:
        """Render standard Prometheus scrape output."""
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )


metrics_service = MetricsService()
