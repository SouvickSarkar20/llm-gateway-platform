"""Unit and integration test suite for Stage 2: Queue, Priority Tiers & Backpressure."""

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.core.database import Base, get_db
from app.models.user import User, UserRole
from app.core.security import get_password_hash
from app.services.llm_gateway import gateway, CircuitState
from app.services.job_service import job_service
from app.services.sqs_producer import sqs_producer, _memory_queues
from app.workers.sqs_consumer import SQSConsumerWorker, worker

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_test_session_factory = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_db():
    async with _test_session_factory() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def setup_test_environment():
    """Setup clean in-memory database, clear memory queues, and reset gateway circuit state."""
    app.dependency_overrides[get_db] = override_get_db

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed test users with different tenant tiers
    async with _test_session_factory() as session:
        users = [
            ("admin", "admin@platform.ai", "admin123", UserRole.ADMIN, "enterprise_tenant", "enterprise"),
            ("user_pro", "pro@platform.ai", "user123", UserRole.USER, "pro_tenant", "pro"),
            ("user_free", "free@platform.ai", "user123", UserRole.USER, "free_tenant", "free"),
        ]
        for username, email, pwd, role, tenant, tier in users:
            u = User(
                username=username,
                email=email,
                hashed_password=get_password_hash(pwd),
                role=role,
                is_active=True,
            )
            u.tenant_id = tenant
            u.tier = tier
            session.add(u)
        await session.commit()

    # Clear memory queues
    for q in _memory_queues.values():
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                break

    # Reset circuit breaker
    async with gateway.primary_circuit._lock:
        gateway.primary_circuit._state = CircuitState.CLOSED
        gateway.primary_circuit._consecutive_failures = 0
        gateway.primary_circuit._consecutive_successes = 0

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_async_mode_returns_202_accepted():
    """Verify POST /chat with async_mode=True enqueues job and returns HTTP 202 Accepted."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Login
        login_res = await client.post(
            "/auth/login",
            json={"username": "user_pro", "password": "user123"},
        )
        token = login_res.json()["access_token"]

        # Submit async chat job
        res = await client.post(
            "/chat",
            json={
                "question": "What is asynchronous queueing?",
                "async_mode": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        assert res.status_code == 202
        data = res.json()
        assert "job_id" in data
        assert data["status"] == "queued"
        assert "/jobs/" in data["poll_url"]
        assert "/stream" in data["stream_url"]
        assert res.headers.get("Location") == data["poll_url"]


@pytest.mark.asyncio
async def test_job_polling_and_worker_execution():
    """Verify worker processes queued job and polling endpoint returns completed result."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/auth/login",
            json={"username": "user_pro", "password": "user123"},
        )
        token = login_res.json()["access_token"]

        # 1. Submit job
        res = await client.post(
            "/chat",
            json={"question": "Explain Little's Law in queuing theory", "async_mode": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 202
        job_id = res.json()["job_id"]

        # 2. Trigger worker execution for next job
        processed = await worker.process_next_job()
        assert processed is True

        # 3. Poll job status endpoint
        poll_res = await client.get(
            f"/jobs/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert poll_res.status_code == 200
        poll_data = poll_res.json()
        assert poll_data["job_id"] == job_id
        assert poll_data["status"] == "completed"
        assert poll_data["result"] is not None
        assert "answer" in poll_data["result"]


@pytest.mark.asyncio
async def test_priority_queue_tier_consumption_order():
    """Verify worker consumes Enterprise (high priority) jobs before Pro (standard) and Free (batch)."""
    # 1. Create 3 jobs for different tiers directly in job_service and producer
    job_free = await job_service.create_job(
        tenant_id="free_tenant",
        tier="free",
        question="Free tier question",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Free tier question"}],
    )
    await sqs_producer.enqueue_job(job_free)

    job_pro = await job_service.create_job(
        tenant_id="pro_tenant",
        tier="pro",
        question="Pro tier question",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Pro tier question"}],
    )
    await sqs_producer.enqueue_job(job_pro)

    job_ent = await job_service.create_job(
        tenant_id="enterprise_tenant",
        tier="enterprise",
        question="Enterprise tier question",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Enterprise tier question"}],
    )
    await sqs_producer.enqueue_job(job_ent)

    # 2. Execute process_next_job 3 times
    # First execution MUST pull the Enterprise job
    await worker.process_next_job()
    ent_state = await job_service.get_job(job_ent["job_id"])
    assert ent_state["status"] == "completed"

    # Second execution MUST pull the Pro job
    await worker.process_next_job()
    pro_state = await job_service.get_job(job_pro["job_id"])
    assert pro_state["status"] == "completed"

    # Third execution MUST pull the Free job
    await worker.process_next_job()
    free_state = await job_service.get_job(job_free["job_id"])
    assert free_state["status"] == "completed"


@pytest.mark.asyncio
async def test_circuit_breaker_open_fast_fails_queued_items():
    """Verify OPEN Circuit Breaker fast-fails queued jobs to prevent stale query backlog."""
    # 1. Enqueue job
    job = await job_service.create_job(
        tenant_id="enterprise_tenant",
        tier="enterprise",
        question="Backpressure test query",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Backpressure test query"}],
    )
    await sqs_producer.enqueue_job(job)

    # 2. Trip Circuit Breaker to OPEN
    async with gateway.primary_circuit._lock:
        gateway.primary_circuit._state = CircuitState.OPEN
        gateway.primary_circuit._last_failure_time = asyncio.get_event_loop().time()

    # 3. Process next job with OPEN circuit
    await worker.process_next_job()

    # 4. Check job status is fast-failed
    job_state = await job_service.get_job(job["job_id"])
    assert job_state["status"] == "failed"
    assert "Circuit Breaker for provider is OPEN" in job_state["error"]


@pytest.mark.asyncio
async def test_sse_streaming_endpoint():
    """Verify GET /jobs/{job_id}/stream returns SSE event stream."""
    # Enqueue a completed job
    job = await job_service.create_job(
        tenant_id="pro_tenant",
        tier="pro",
        question="SSE stream test",
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "SSE stream test"}],
    )
    await job_service.update_job(job["job_id"], status="completed", result={"answer": "SSE Output"})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/auth/login",
            json={"username": "user_pro", "password": "user123"},
        )
        token = login_res.json()["access_token"]

        res = await client.get(
            f"/jobs/{job['job_id']}/stream",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]
        assert "event: status_update" in res.text or "event: done" in res.text
