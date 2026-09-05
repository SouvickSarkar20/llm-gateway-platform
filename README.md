# LLM Gateway Platform

A resilient, production-ready asynchronous LLM Gateway and AI Chat Platform built with **FastAPI**, **SQLAlchemy 2.0 (Async)**, **Redis**, and **Prometheus Telemetry**.

---

## 🚀 Key Features

- **⚡ Asynchronous Architecture**: Non-blocking I/O throughout the stack using FastAPI and async SQLAlchemy with connection pooling.
- **🛡️ Fault Tolerance & Resilience**:
  - Multi-provider LLM orchestration with automatic fallback mechanisms.
  - Circuit Breakers and exponential backoff retries via Tenacity.
  - Graceful degradation when external AI APIs experience latency or outages.
- **🔐 Security & Access Control**:
  - JWT-based authentication (HS256) with passlib/bcrypt password hashing.
  - Role-Based Access Control (`ADMIN`, `USER`, `READ_ONLY`).
- **🏎️ High-Performance Caching & Rate Limiting**:
  - Distributed token-bucket / burst-capable rate limiting powered by Redis.
  - Query and response caching with configurable TTLs.
- **📊 Observability & Usage Analytics**:
  - Token consumption tracking, latency recording, and cost accounting per user/request.
  - Prometheus metrics instrumentation (`/metrics`) for real-time monitoring.

---

## 📁 Project Structure

```text
├── app/
│   ├── core/
│   │   ├── database.py         # Async SQLAlchemy engine & session management
│   │   ├── redis.py            # Async Redis client & cache operations
│   │   └── security.py         # JWT tokens, password hashing & auth helpers
│   ├── models/
│   │   ├── user.py             # User & RBAC ORM models
│   │   └── usage.py            # LLM token usage tracking & analytics models
│   └── config.py               # Pydantic Settings loaded from environment
├── scripts/
│   └── init_db.py              # Database initialization and user seeding
├── tests/
│   └── test_phase1_database.py # Database and async session test suites
├── .env.example                # Template for environment configuration
├── pyrightconfig.json          # Python static type checker configuration
├── requirements.txt            # Python dependencies
└── README.md
```

---

## 🛠️ Quickstart Guide

### 1. Prerequisites
- Python 3.10+
- Redis (optional for mock mode, required for distributed caching/rate-limiting)
- SQLite (default for local development) or PostgreSQL

### 2. Setup Virtual Environment
```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Configure Environment
Copy the example environment file:
```bash
cp .env.example .env
```

Review and adjust settings in `.env` as needed:
```ini
APP_NAME=AI-Platform-QA-API
DATABASE_URL=sqlite+aiosqlite:///./chat_platform.db
REDIS_URL=redis://localhost:6379/0
LLM_PROVIDER=mock
```

### 5. Initialize the Database
Seed default users (`admin`, `user`, `readonly`) and create ORM tables:
```bash
python scripts/init_db.py
```

### 6. Run Automated Tests
```bash
pytest
```

---

## 🔒 Security Note
Do **not** commit `.env` or sensitive API keys to source control. Use environment variables or secret managers in production environments.
