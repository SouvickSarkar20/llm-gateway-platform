"""Unit tests for Phase 1: Database models, users, and usage logs."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select
from app.core.database import Base
from app.models.user import User, UserRole
from app.models.usage import LLMUsageLog, RequestStatus
from app.core.security import verify_password, get_password_hash
from app.config import get_settings


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest_asyncio.fixture
async def test_db_session():
    """Create in-memory SQLite database for testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_maker() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_user_creation_and_password_verification(test_db_session: AsyncSession):
    """Test creating a user with hashed password and verifying credentials."""
    password = "SuperSecretPassword123!"
    hashed = get_password_hash(password)

    user = User(
        username="dev_engineer",
        email="dev@company.com",
        hashed_password=hashed,
        role=UserRole.ADMIN,
        is_active=True,
    )
    test_db_session.add(user)
    await test_db_session.commit()

    stmt = select(User).where(User.username == "dev_engineer")
    result = await test_db_session.execute(stmt)
    persisted_user = result.scalar_one_or_none()

    assert persisted_user is not None
    assert persisted_user.email == "dev@company.com"
    assert persisted_user.role == UserRole.ADMIN
    assert verify_password(password, persisted_user.hashed_password) is True
    assert verify_password("WrongPassword", persisted_user.hashed_password) is False


@pytest.mark.asyncio
async def test_llm_usage_log_persistence(test_db_session: AsyncSession, settings):
    """Test inserting and querying persistent token and cost usage log."""
    cost = settings.calculate_cost("gpt-4o-mini", prompt_tokens=150, completion_tokens=80)

    log_entry = LLMUsageLog(
        request_id="req-uuid-12345",
        user_id="usr-uuid-67890",
        model="gpt-4o-mini",
        provider="mock",
        prompt_tokens=150,
        completion_tokens=80,
        total_tokens=230,
        estimated_cost_usd=cost,
        latency_ms=124.5,
        cache_hit=False,
        is_fallback=False,
        status=RequestStatus.SUCCESS,
    )
    test_db_session.add(log_entry)
    await test_db_session.commit()

    stmt = select(LLMUsageLog).where(LLMUsageLog.request_id == "req-uuid-12345")
    result = await test_db_session.execute(stmt)
    persisted_log = result.scalar_one_or_none()

    assert persisted_log is not None
    assert persisted_log.total_tokens == 230
    assert persisted_log.estimated_cost_usd == cost
    assert persisted_log.status == RequestStatus.SUCCESS
    assert persisted_log.cache_hit is False
