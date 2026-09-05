"""Comprehensive integration tests for Phase 5: /chat, /health, /metrics, and DB audit logging."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select

from app.main import app
from app.core.database import Base, get_db
from app.models.user import User, UserRole
from app.models.usage import LLMUsageLog, RequestStatus
from app.core.security import get_password_hash
from app.services.llm_gateway import gateway, MockLLMProvider

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_test_session_factory = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_db():
    async with _test_session_factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def setup_test_database():
    """Seed test database before each test."""
    app.dependency_overrides[get_db] = override_get_db

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _test_session_factory() as session:
        users = [
            ("admin", "admin@platform.ai", "admin123", UserRole.ADMIN),
            ("user", "user@platform.ai", "user123", UserRole.USER),
            ("readonly", "readonly@platform.ai", "readonly123", UserRole.READ_ONLY),
        ]
        for username, email, pwd, role in users:
            session.add(
                User(
                    username=username,
                    email=email,
                    hashed_password=get_password_hash(pwd),
                    role=role,
                    is_active=True,
                )
            )
        await session.commit()

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_check_endpoint():
    """Verify GET /health returns 200 with DB and Redis connected."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"
        assert data["redis"] == "connected"


@pytest.mark.asyncio
async def test_prometheus_metrics_endpoint():
    """Verify GET /metrics returns standard Prometheus scrape format."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/metrics")
        assert res.status_code == 200
        text = res.text
        # Check standard Prometheus elements
        assert "# HELP" in text
        assert "# TYPE" in text
        assert "llm_tokens_total" in text or "http_requests_total" in text or "llm_circuit_breaker_state" in text


@pytest.mark.asyncio
async def test_chat_unauthenticated():
    """Verify POST /chat rejects unauthenticated requests with 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/chat", json={"question": "What is AI?"})
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_chat_rbac_readonly_forbidden():
    """Verify POST /chat rejects readonly users with 403 Forbidden."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login as readonly
        login_res = await client.post(
            "/auth/login",
            json={"username": "readonly", "password": "readonly123"},
        )
        token = login_res.json()["access_token"]

        res = await client.post(
            "/chat",
            json={"question": "What is AI?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 403
        assert "Access forbidden" in res.json()["detail"]


@pytest.mark.asyncio
async def test_chat_happy_path_and_db_persistence():
    """Verify POST /chat executes generation, computes cost, and persists row in database."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Login as user
        login_res = await client.post(
            "/auth/login",
            json={"username": "user", "password": "user123"},
        )
        token = login_res.json()["access_token"]

        # 2. First call (Cache MISS)
        res = await client.post(
            "/chat",
            json={"question": "Explain database indexing principles", "use_cache": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert "database indexing" in data["answer"].lower()
        assert data["cache_hit"] is False
        assert data["usage"]["total_tokens"] > 0
        assert data["usage"]["estimated_cost_usd"] > 0
        assert res.headers.get("X-Cache") == "MISS"
        req_id = data["request_id"]

        # 3. Check database record
        async with _test_session_factory() as session:
            stmt = select(LLMUsageLog).where(LLMUsageLog.request_id == req_id)
            result = await session.execute(stmt)
            db_log = result.scalar_one_or_none()

            assert db_log is not None
            assert db_log.total_tokens == data["usage"]["total_tokens"]
            assert db_log.estimated_cost_usd == data["usage"]["estimated_cost_usd"]
            assert db_log.status == RequestStatus.SUCCESS
            assert db_log.cache_hit is False

        # 4. Second call with exact same question -> Cache HIT!
        res_cached = await client.post(
            "/chat",
            json={"question": "Explain database indexing principles", "use_cache": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res_cached.status_code == 200
        data_cached = res_cached.json()
        assert data_cached["cache_hit"] is True
        assert data_cached["usage"]["total_tokens"] == 0
        assert data_cached["usage"]["estimated_cost_usd"] == 0.0
        assert res_cached.headers.get("X-Cache") == "HIT"


@pytest.mark.asyncio
async def test_chat_fallback_trigger_on_primary_down():
    """Verify /chat triggers fallback model when primary provider fails."""
    # Temporarily set mock provider to fail 5 times
    mock_prov = MockLLMProvider()
    mock_prov.set_fault("server_error", count=5)
    gateway._providers["mock"] = mock_prov

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/auth/login",
            json={"username": "user", "password": "user123"},
        )
        token = login_res.json()["access_token"]

        res = await client.post(
            "/chat",
            json={"question": "Simulate primary failure for fallback test", "use_cache": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["is_fallback"] is True
        assert res.headers.get("X-Fallback-Triggered") == "true"
