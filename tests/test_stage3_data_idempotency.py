"""Unit and Integration tests for Stage 3: Data Layer & Idempotency."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select

from app.main import app
from app.core.database import Base, get_db
from app.models.tenant import Tenant, ApiKey, TenantQuota
from app.models.user import User, UserRole
from app.models.usage import LLMUsageLog, RequestStatus
from app.core.security import get_password_hash, create_access_token
from app.services.idempotency_service import IdempotencyService

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
        user = User(
            username="testuser",
            email="testuser@platform.ai",
            hashed_password=get_password_hash("password123"),
            role=UserRole.USER,
            is_active=True,
        )
        session.add(user)
        await session.commit()

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def test_db_session():
    async with _test_session_factory() as session:
        yield session


@pytest.mark.asyncio
async def test_tenant_api_key_quota_models(test_db_session: AsyncSession):
    """Verify creation and query of Tenant, ApiKey, and TenantQuota models in DB."""
    tenant = Tenant(name="Acme Corp", tier="enterprise")
    test_db_session.add(tenant)
    await test_db_session.flush()

    api_key = ApiKey(key="sk-ent-1234567890", tenant_id=tenant.id, name="Prod Key")
    quota = TenantQuota(
        tenant_id=tenant.id, max_monthly_tokens=5000000, rate_limit_rpm=300, burst_capacity=50
    )
    test_db_session.add_all([api_key, quota])
    await test_db_session.commit()

    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.api_keys), selectinload(Tenant.quota))
        .where(Tenant.name == "Acme Corp")
    )
    result = await test_db_session.execute(stmt)
    saved_tenant = result.scalar_one()

    assert saved_tenant is not None
    assert saved_tenant.tier == "enterprise"
    assert len(saved_tenant.api_keys) == 1
    assert saved_tenant.api_keys[0].key == "sk-ent-1234567890"
    # pyrefly: ignore [missing-attribute]
    assert saved_tenant.quota.rate_limit_rpm == 300


@pytest.mark.asyncio
async def test_usage_log_with_tenant_id(test_db_session: AsyncSession):
    """Verify LLMUsageLog persists tenant_id correctly."""
    log = LLMUsageLog(
        request_id="req-test-123",
        user_id="user-123",
        tenant_id="tenant-acme-456",
        model="gpt-4o-mini",
        provider="mock",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        estimated_cost_usd=0.00005,
        latency_ms=45.0,
        status=RequestStatus.SUCCESS,
    )
    test_db_session.add(log)
    await test_db_session.commit()

    stmt = select(LLMUsageLog).where(LLMUsageLog.request_id == "req-test-123")
    res = await test_db_session.execute(stmt)
    saved_log = res.scalar_one()

    assert saved_log.tenant_id == "tenant-acme-456"
    assert saved_log.total_tokens == 30


@pytest.mark.asyncio
async def test_idempotency_service_lifecycle():
    """Verify get_or_lock, save_completed, and lock release lifecycle."""
    svc = IdempotencyService()
    identifier = "tenant-idemp-test"
    key = "key-unique-001"

    state, payload = await svc.get_or_lock(identifier, key)
    assert state == "NEW"
    assert payload is None

    state2, payload2 = await svc.get_or_lock(identifier, key)
    assert state2 == "PROCESSING"
    assert payload2 is None

    mock_resp = {"answer": "Hello World", "model_used": "gpt-4o-mini"}
    saved = await svc.save_completed(identifier, key, mock_resp)
    assert saved is True

    state3, payload3 = await svc.get_or_lock(identifier, key)
    assert state3 == "COMPLETED"
    assert payload3 == mock_resp


@pytest.mark.asyncio
async def test_idempotency_http_replay(test_db_session: AsyncSession):
    """Integration test verifying HTTP header Idempotency-Key replay."""
    stmt = select(User).where(User.username == "testuser")
    res = await test_db_session.execute(stmt)
    user = res.scalar_one()

    token = create_access_token(subject=user.id, role=user.role.value)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "req-idemp-http-999",
    }
    payload = {"question": "What is Idempotency in REST APIs?", "use_cache": False}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. First HTTP POST request
        resp1 = await client.post("/api/v1/chat", json=payload, headers=headers)
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert "answer" in data1
        assert "X-Idempotent-Replay" not in resp1.headers

        # 2. Second HTTP POST request with exact same Idempotency-Key
        resp2 = await client.post("/api/v1/chat", json=payload, headers=headers)
        assert resp2.status_code == 200
        data2 = resp2.json()

        # Verify identical response payload and X-Idempotent-Replay header
        assert data2["answer"] == data1["answer"]
        assert resp2.headers.get("X-Idempotent-Replay") == "true"
