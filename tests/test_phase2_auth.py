"""Unit and integration tests for Phase 2: Authentication & RBAC."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.main import app
from app.core.database import Base, get_db, init_db
from app.core.security import create_access_token
from app.models.user import UserRole
from app.api.deps import require_admin, require_user_or_admin


# In-memory test engine
test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
_test_session_factory = async_sessionmaker(
    bind=test_engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_db():
    async with _test_session_factory() as session:
        yield session


# Add dummy test routes to verify RBAC guards
@app.get("/test-admin-only", dependencies=[pytest.importorskip("fastapi").Depends(require_admin)])
async def dummy_admin_route():
    return {"message": "Welcome Admin"}


@app.get("/test-user-or-admin", dependencies=[pytest.importorskip("fastapi").Depends(require_user_or_admin)])
async def dummy_user_route():
    return {"message": "Welcome User"}


@pytest_asyncio.fixture(autouse=True)
async def setup_test_database():
    """Create schema and seed default users in memory before running tests."""
    app.dependency_overrides[get_db] = override_get_db

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed users using test session
    from app.models.user import User
    from app.core.security import get_password_hash

    async with _test_session_factory() as session:
        seed_data = [
            ("admin", "admin@platform.ai", "admin123", UserRole.ADMIN),
            ("user", "user@platform.ai", "user123", UserRole.USER),
            ("readonly", "readonly@platform.ai", "readonly123", UserRole.READ_ONLY),
        ]
        for username, email, pwd, role in seed_data:
            user = User(
                username=username,
                email=email,
                hashed_password=get_password_hash(pwd),
                role=role,
                is_active=True,
            )
            session.add(user)
        await session.commit()

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_success():
    """Test login with valid user credentials."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"username": "user", "password": "user123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["username"] == "user"
        assert data["role"] == "user"


@pytest.mark.asyncio
async def test_login_invalid_password():
    """Test login failure with incorrect password."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"username": "user", "password": "WrongPassword999!"},
        )
        assert response.status_code == 401
        assert "Incorrect username or password" in response.json()["detail"]


@pytest.mark.asyncio
async def test_login_nonexistent_user():
    """Test login failure with non-existent user."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/auth/login",
            json={"username": "ghost_user", "password": "anypassword"},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_profile_with_token():
    """Test GET /auth/me with valid Bearer token."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin123"},
        )
        token = login_res.json()["access_token"]

        response = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        profile = response.json()
        assert profile["username"] == "admin"
        assert profile["role"] == "admin"
        assert profile["email"] == "admin@platform.ai"


@pytest.mark.asyncio
async def test_get_me_unauthorized():
    """Test GET /auth/me with missing or invalid token."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing token
        res_no_token = await client.get("/auth/me")
        assert res_no_token.status_code == 401

        # Invalid token
        res_bad_token = await client.get(
            "/auth/me",
            headers={"Authorization": "Bearer invalid.token.signature"},
        )
        assert res_bad_token.status_code == 401


@pytest.mark.asyncio
async def test_rbac_access_control():
    """Verify RBAC: Admin can access admin routes; User cannot; Readonly cannot access user routes."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Login as Admin
        admin_login = await client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin123"},
        )
        admin_token = admin_login.json()["access_token"]

        # 2. Login as regular User
        user_login = await client.post(
            "/auth/login",
            json={"username": "user", "password": "user123"},
        )
        user_token = user_login.json()["access_token"]

        # 3. Login as Read-Only
        ro_login = await client.post(
            "/auth/login",
            json={"username": "readonly", "password": "readonly123"},
        )
        ro_token = ro_login.json()["access_token"]

        # Admin accessing admin-only endpoint -> 200 OK
        res = await client.get("/test-admin-only", headers={"Authorization": f"Bearer {admin_token}"})
        assert res.status_code == 200

        # Regular user accessing admin-only endpoint -> 403 Forbidden
        res = await client.get("/test-admin-only", headers={"Authorization": f"Bearer {user_token}"})
        assert res.status_code == 403
        assert "Access forbidden" in res.json()["detail"]

        # Regular user accessing user endpoint -> 200 OK
        res = await client.get("/test-user-or-admin", headers={"Authorization": f"Bearer {user_token}"})
        assert res.status_code == 200

        # Read-only accessing user endpoint -> 403 Forbidden
        res = await client.get("/test-user-or-admin", headers={"Authorization": f"Bearer {ro_token}"})
        assert res.status_code == 403


@pytest.mark.asyncio
async def test_user_registration():
    """Test registering a new user and authenticating with the new credentials."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reg_res = await client.post(
            "/auth/register",
            json={
                "username": "new_engineer",
                "email": "engineer@platform.ai",
                "password": "SecurePassword456!",
            },
        )
        assert reg_res.status_code == 201
        data = reg_res.json()
        assert data["username"] == "new_engineer"
        assert data["role"] == "user"

        # Attempt to login with newly created credentials
        login_res = await client.post(
            "/auth/login",
            json={"username": "new_engineer", "password": "SecurePassword456!"},
        )
        assert login_res.status_code == 200
        assert "access_token" in login_res.json()
