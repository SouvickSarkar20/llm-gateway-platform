# AI LLM Gateway Platform

A production-ready AI Question-Answering API built with FastAPI, Redis, PostgreSQL, and a resilient LLM Gateway. Implements JWT authentication, role-based access control, distributed rate limiting, circuit breaking, and Prometheus observability.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [API Reference](#api-reference)
- [Authentication and RBAC](#authentication-and-rbac)
- [LLM Gateway Resilience](#llm-gateway-resilience)
- [Prerequisites](#prerequisites)
- [Configuration](#configuration)
- [Running Locally](#running-locally)
- [Running with Docker Compose](#running-with-docker-compose)
- [Kubernetes Deployment](#kubernetes-deployment)
- [Running Tests](#running-tests)
- [Monitoring](#monitoring)
- [Default Credentials](#default-credentials)
- [Project Structure](#project-structure)
- [Scaling and Architecture Decisions](#scaling-and-architecture-decisions)

---

## Overview

This platform accepts a user question via HTTP, authenticates the request using a JWT bearer token, routes the query through a resilient LLM Gateway (supporting OpenAI, Google Gemini, or a deterministic mock provider), persists token usage and cost to PostgreSQL, caches responses in Redis, and exposes Prometheus metrics for observability.

Core Capabilities:

- JWT authentication with RBAC (Admin, User, Read-Only roles)
- POST /chat with caching, retry logic, and fallback model support
- Circuit breaker (3-state: CLOSED, HALF_OPEN, OPEN) per provider
- Exponential backoff with full jitter on retryable errors
- Distributed sliding-window rate limiting via Redis sorted sets
- Per-request token usage and cost tracking persisted to PostgreSQL
- Prometheus metrics: LLM latency, token counts, cache hit/miss, circuit breaker state
- Multi-stage Docker build with a non-root runtime user
- Kubernetes manifests with HPA scaling from 3 to 30 replicas
- 5 test suites covering database, auth, LLM gateway, Redis, and the chat API

---

## Architecture

```
Client
  |
  v
FastAPI Application (Uvicorn + asyncio)
  |-- POST /auth/login     -> JWT issuance
  |-- POST /chat           -> Rate limiter -> Redis cache -> LLM Gateway -> PostgreSQL
  |-- GET  /health         -> PostgreSQL + Redis liveness probes
  |-- GET  /metrics        -> Prometheus scrape endpoint
  |
  |-- Redis 7              (sliding-window rate limiter, response cache, TTL: 1h)
  |-- PostgreSQL 16        (users table, llm_usage_logs table)
  |-- LLM Gateway
        |-- Primary model  (OpenAI gpt-4o-mini or Gemini 1.5 Flash)
        |-- Fallback model (OpenAI gpt-3.5-turbo or Gemini 1.5 Flash)
        |-- Circuit Breaker per provider
        |-- Exponential backoff + jitter (up to 3 retries)
```

For detailed architecture diagrams, scaling analysis, SSO/OIDC integration plan, migration roadmap, and failure mode analysis, see ARCHITECTURE.md.

---

## API Reference

All endpoints are mounted at the root path without a version prefix to match the assessment specification.

### POST /auth/login

Authenticate and receive a JWT bearer token.

Request body:
```json
{
  "username": "admin",
  "password": "admin123"
}
```

Response:
```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 3600,
  "user_id": "<uuid>",
  "username": "admin",
  "role": "admin"
}
```

---

### POST /chat

Submit a question to the LLM Gateway. Requires a valid bearer token for a User or Admin role.

Headers:
```
Authorization: Bearer <access_token>
```

Request body:
```json
{
  "question": "What is a circuit breaker pattern?",
  "model": "gpt-4o-mini",
  "temperature": 0.7,
  "max_tokens": 1024,
  "use_cache": true
}
```

Alternatively, pass a structured `messages` array (OpenAI chat format) instead of `question`.

Response:
```json
{
  "answer": "A circuit breaker is ...",
  "model_used": "gpt-4o-mini",
  "provider": "openai",
  "is_fallback": false,
  "cache_hit": false,
  "usage": {
    "prompt_tokens": 42,
    "completion_tokens": 185,
    "total_tokens": 227,
    "estimated_cost_usd": 0.0001234,
    "latency_ms": 1247.5
  },
  "request_id": "<uuid>"
}
```

Response headers:

| Header | Description |
|---|---|
| `X-Cache` | `HIT` or `MISS` |
| `X-Model-Used` | Model that produced the response |
| `X-Fallback-Triggered` | Present and `true` when the fallback model was used |
| `X-RateLimit-Limit` | Configured request quota per window |
| `X-RateLimit-Remaining` | Requests remaining in the current window |
| `X-RateLimit-Reset` | Seconds until the window resets |
| `Retry-After` | Present on 503 when the circuit breaker is OPEN |

---

### GET /health

Deep dependency health check. Returns 200 when both PostgreSQL and Redis are reachable; 503 otherwise.

```json
{
  "status": "healthy",
  "database": "connected",
  "redis": "connected",
  "timestamp": "2024-01-01T00:00:00Z",
  "version": "1.0.0",
  "issues": []
}
```

---

### GET /metrics

Prometheus-format scrape endpoint. Returns standard `text/plain` Prometheus exposition format.

Custom metrics exposed:

| Metric | Type | Description |
|---|---|---|
| `llm_gateway_requests_total` | Counter | Total LLM requests by model, provider, status |
| `llm_tokens_total` | Counter | Prompt and completion tokens consumed |
| `llm_request_duration_seconds` | Histogram | LLM API call latency |
| `llm_circuit_breaker_state` | Gauge | Circuit state: 0=CLOSED, 1=HALF_OPEN, 2=OPEN |
| `llm_cache_hits_total` | Counter | Requests served from Redis cache |
| `llm_cache_misses_total` | Counter | Requests requiring fresh LLM generation |
| `llm_rate_limit_exceeded_total` | Counter | Requests rejected by rate limiter |
| `http_requests_total` | Counter | HTTP requests by method, endpoint, status code |
| `http_request_duration_seconds` | Histogram | End-to-end request latency |

---

### Additional Endpoints

| Method | Path | Description |
|---|---|---|
| POST | /auth/register | Register a new user account (USER role by default) |
| GET | /auth/me | Return the profile of the authenticated user |
| GET | / | Service metadata and link index |
| GET | /docs | Swagger UI interactive documentation |
| GET | /redoc | ReDoc API documentation |

---

## Authentication and RBAC

Authentication uses HS256-signed JWT tokens. Each token carries a `sub` (user ID), `role`, `username`, `exp`, `iat`, and `nbf` claim.

Role permissions:

| Endpoint | Admin | User | Read-Only |
|---|---|---|---|
| POST /auth/login, /auth/register | Yes | Yes (self) | No |
| GET /auth/me | Yes | Yes | Yes |
| POST /chat | Yes | Yes (rate-limited) | No (403) |
| GET /health | Yes | Yes | Yes |
| GET /metrics | Yes | No (403) | No (403) |

The `/auth/register` endpoint intentionally prevents self-assignment of the Admin role. Admin accounts must be created by seeding or by an existing administrator.

For SSO/OIDC extension details (Okta, Keycloak, Azure Entra ID, PKCE flows, JWKS key rotation, API Gateway token offloading), see Section 1 of ARCHITECTURE.md.

---

## LLM Gateway Resilience

The `ResilientLLMGateway` class (`app/services/llm_gateway.py`) orchestrates:

1. Circuit Breaker: 3-state machine (CLOSED / HALF_OPEN / OPEN) per provider. Trips after 5 consecutive failures; enters HALF_OPEN after a 30-second recovery timeout; closes after 2 successful probe requests.

2. Exponential Backoff with Jitter: `sleep = min(max_backoff, base * 2^attempt) + uniform_jitter`. Default: base 0.5s, max 4.0s, jitter factor 0.25. Up to 3 retries per provider.

3. Error Classification: Timeout (504), rate limit (429), and 5xx errors are retryable. 4xx client errors (bad prompt, invalid key) fail fast without retry.

4. Model Fallback: When the primary model's circuit is OPEN or retries are exhausted, requests are routed to the fallback model on the same provider. If both fail, a 503 is returned.

5. Redis Cache-Aside: Responses are cached by SHA-256 hash of `(question, model)`. Cache TTL defaults to 3600 seconds. Cache reads bypass the LLM entirely and log a zero-cost usage record.

Supported providers:

| Provider | Value | Notes |
|---|---|---|
| Mock | `mock` | Default. Deterministic responses, no API key required. Supports fault injection for testing. |
| OpenAI | `openai` | Requires `OPENAI_API_KEY`. Models: gpt-4o-mini (primary), gpt-3.5-turbo (fallback). |
| Gemini | `gemini` | Requires `GEMINI_API_KEY`. Models: gemini-1.5-flash. |

---

## Prerequisites

- Python 3.10 or higher
- Docker and Docker Compose (for containerized deployment)
- Redis 7 (for local development without Docker)
- PostgreSQL 16 (for local development without Docker; SQLite is used by default)

---

## Configuration

Copy `.env.example` to `.env` and set the required values:

```bash
cp .env.example .env
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `mock` | Active provider: `mock`, `openai`, or `gemini` |
| `OPENAI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=openai` |
| `GEMINI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=gemini` |
| `JWT_SECRET_KEY` | _(dev value)_ | Must be replaced with a 32+ character secret in production |
| `DATABASE_URL` | `sqlite+aiosqlite:///./chat_platform.db` | PostgreSQL URL for Docker: `postgresql+asyncpg://user:pass@db:5432/chat_platform` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Sliding-window quota per user/IP |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures before circuit opens |
| `MAX_RETRIES` | `3` | Maximum retry attempts per provider |

Generate a secure JWT secret key:
```bash
openssl rand -hex 32
```

---

## Running Locally

Install dependencies:
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Start the server (SQLite + mock LLM, no external dependencies):
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at http://localhost:8000. Interactive docs are at http://localhost:8000/docs.

Default seed accounts are created on first startup. See Default Credentials.

---

## Running with Docker Compose

The compose stack includes the FastAPI application, PostgreSQL 16, Redis 7, and Prometheus.

Start all services:
```bash
docker compose up --build
```

Start in detached mode:
```bash
docker compose up --build -d
```

Service ports:

| Service | Port | Description |
|---|---|---|
| FastAPI API | 8000 | Main application |
| PostgreSQL | 5432 | Database |
| Redis | 6379 | Cache and rate limiter |
| Prometheus | 9090 | Metrics scraper |

To use a real LLM provider with Docker Compose, set the relevant variables in your `.env` file before running:

```bash
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

Stop and remove containers:
```bash
docker compose down
```

Stop and remove containers along with volumes (clears all data):
```bash
docker compose down -v
```

---

## Kubernetes Deployment

Manifests are located in the `k8s/` directory.

Files:

| File | Description |
|---|---|
| `k8s/deployment.yaml` | FastAPI deployment: 3 replicas, rolling update, liveness/readiness/startup probes |
| `k8s/service.yaml` | ClusterIP service exposing port 8000 |
| `k8s/hpa.yaml` | HorizontalPodAutoscaler: 3-30 replicas, CPU target 60%, memory target 75% |
| `k8s/configmap.yaml` | Non-sensitive environment configuration |
| `k8s/secret.yaml` | Template for sensitive values (JWT secret, API keys, database credentials) |

Apply manifests:
```bash
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
```

Before applying, update `k8s/secret.yaml` with base64-encoded production values. Never commit actual secrets to version control.

HPA behavior:
- Scale-up: up to 100% pod expansion every 15 seconds (aggressively tracks traffic spikes)
- Scale-down: maximum 10% reduction per 60 seconds with a 300-second stabilization window (prevents thrashing)

---

## Running Tests

Tests use `fakeredis` and `aiosqlite` (in-memory SQLite). No external services are required.

Run the full test suite:
```bash
pytest tests/ -v
```

Run a specific test phase:
```bash
pytest tests/test_phase1_database.py -v
pytest tests/test_phase2_auth.py -v
pytest tests/test_phase3_llm_gateway.py -v
pytest tests/test_phase4_redis.py -v
pytest tests/test_phase5_chat_api.py -v
```

Test coverage by phase:

| Phase | File | Coverage |
|---|---|---|
| 1 | `test_phase1_database.py` | Database initialization, schema creation, user seeding |
| 2 | `test_phase2_auth.py` | Login, token validation, RBAC enforcement, inactive accounts |
| 3 | `test_phase3_llm_gateway.py` | Circuit breaker state transitions, retry logic, fault injection |
| 4 | `test_phase4_redis.py` | Sliding-window rate limiter, cache set/get/TTL |
| 5 | `test_phase5_chat_api.py` | End-to-end chat flow, cache hits, rate limiting, error responses |

---

## Monitoring

When running via Docker Compose, Prometheus scrapes the `/metrics` endpoint every 5 seconds.

Access Prometheus at http://localhost:9090.

Useful queries:

```promql
# LLM request rate by model
rate(llm_gateway_requests_total[1m])

# p95 LLM response latency
histogram_quantile(0.95, rate(llm_request_duration_seconds_bucket[5m]))

# Cache hit ratio
rate(llm_cache_hits_total[5m]) / (rate(llm_cache_hits_total[5m]) + rate(llm_cache_misses_total[5m]))

# Circuit breaker state (2 = OPEN)
llm_circuit_breaker_state

# Total tokens consumed
increase(llm_tokens_total[1h])
```

For production deployments, connect Grafana to Prometheus and configure alerting rules on circuit breaker state changes, p99 latency exceeding thresholds, and error rate spikes.

---

## Default Credentials

These accounts are seeded automatically on first startup.

| Username | Password | Role | Permissions |
|---|---|---|---|
| `admin` | `admin123` | Admin | All endpoints including /metrics, user management |
| `user` | `user123` | User | POST /chat (rate-limited), /health, /auth/me |
| `readonly` | `readonly123` | Read-Only | GET /health, GET /auth/me only |

Change these credentials immediately in any environment accessible beyond localhost.

---

## Project Structure

```
.
|-- app/
|   |-- main.py                     # Application entry point, middleware, router registration
|   |-- config.py                   # Pydantic Settings: all configuration via environment variables
|   |-- api/
|   |   |-- deps.py                 # Shared dependencies: auth, RBAC guards, rate limiter guard
|   |   `-- v1/
|   |       |-- auth.py             # POST /auth/login, POST /auth/register, GET /auth/me
|   |       |-- chat.py             # POST /chat
|   |       |-- health.py           # GET /health
|   |       `-- metrics.py          # GET /metrics
|   |-- core/
|   |   |-- database.py             # SQLAlchemy async engine, session factory, schema init, seeding
|   |   |-- redis.py                # Redis async client with connection pooling and health probe
|   |   `-- security.py             # bcrypt password hashing, JWT issuance and decoding
|   |-- models/
|   |   |-- user.py                 # User ORM model, UserRole enum
|   |   `-- usage.py                # LLMUsageLog ORM model, RequestStatus enum
|   |-- schemas/
|   |   |-- auth.py                 # Pydantic request/response schemas for auth endpoints
|   |   `-- chat.py                 # ChatRequest, ChatResponse, UsageStats schemas
|   `-- services/
|       |-- llm_gateway.py          # ResilientLLMGateway, CircuitBreaker, provider implementations
|       |-- cache_service.py        # Redis cache-aside read/write with key hashing
|       |-- rate_limiter.py         # DistributedRateLimiter using Redis sorted sets
|       `-- metrics_service.py      # Prometheus counter/histogram/gauge instrumentation
|-- tests/
|   |-- conftest.py                 # Global test fixtures, environment patching
|   |-- test_phase1_database.py
|   |-- test_phase2_auth.py
|   |-- test_phase3_llm_gateway.py
|   |-- test_phase4_redis.py
|   `-- test_phase5_chat_api.py
|-- k8s/
|   |-- deployment.yaml
|   |-- service.yaml
|   |-- hpa.yaml
|   |-- configmap.yaml
|   `-- secret.yaml
|-- scripts/
|   `-- init_db.py                  # Standalone database initialization utility
|-- Dockerfile                      # Multi-stage build: builder + slim runtime, non-root user
|-- docker-compose.yml              # FastAPI + PostgreSQL + Redis + Prometheus
|-- prometheus.yml                  # Prometheus scrape configuration
|-- requirements.txt                # Pinned Python dependencies
|-- .env.example                    # Environment variable template
|-- ARCHITECTURE.md                 # SSO/OIDC design, scaling analysis, migration plan
`-- README.md
```

---

## Scaling and Architecture Decisions

The platform is designed for horizontal scaling. Key decisions:

Stateless API Instances: All shared state (rate limit counters, response cache) lives in Redis. Any number of FastAPI pods can run behind a load balancer without sticky sessions.

Async Throughout: SQLAlchemy async engine, async Redis client, and httpx async HTTP calls mean a single Uvicorn worker can handle many concurrent in-flight LLM requests without blocking.

Fail-Open on Redis Failure: If Redis becomes unavailable, the rate limiter allows requests through and the cache is bypassed. The platform remains operational at the cost of cache efficiency and rate limiting precision.

Circuit Breaker Prevents Cascading Failures: When an upstream LLM provider becomes slow or returns errors, the circuit opens and requests fail-fast (503) rather than accumulating in-flight connections and exhausting the worker pool.

Fallback Model as Degraded-Mode Operation: A second model is configured as fallback. If the primary model's circuit opens, the fallback model's circuit is attempted before returning a 503 to the client.

For a detailed analysis of the 100-500 RPS scaling scenario, Little's Law capacity calculations, HPA configuration rationale, Redis cluster topology, and the EC2-to-EKS zero-downtime migration playbook, refer to ARCHITECTURE.md.
