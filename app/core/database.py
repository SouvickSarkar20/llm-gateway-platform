"""Database configuration and async SQLAlchemy session management."""

import logging
from typing import Any, AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import select
from app.config import get_settings

logger = logging.getLogger("chat_platform.database")
settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""
    pass


# Build database engine with conditional pooling based on DB dialect
engine_kwargs: dict[str, Any] = {"echo": settings.DB_ECHO}
if "sqlite" not in settings.DATABASE_URL:
    engine_kwargs.update(
        {
            "pool_size": settings.DB_POOL_SIZE,
            "max_overflow": settings.DB_MAX_OVERFLOW,
            "pool_pre_ping": True,
        }
    )

engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    **engine_kwargs,
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for obtaining an asynchronous database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables and seed default RBAC users."""
    from app.models.user import User, UserRole
    from app.models.usage import LLMUsageLog  # Ensure model is registered
    from app.core.security import get_password_hash

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed default users if not already present
    async with async_session_factory() as session:
        seed_users = [
            ("admin", "admin@platform.ai", "admin123", UserRole.ADMIN),
            ("user", "user@platform.ai", "user123", UserRole.USER),
            ("readonly", "readonly@platform.ai", "readonly123", UserRole.READ_ONLY),
        ]

        for username, email, password, role in seed_users:
            stmt = select(User).where(User.username == username)
            result = await session.execute(stmt)
            existing_user = result.scalar_one_or_none()

            if not existing_user:
                new_user = User(
                    username=username,
                    email=email,
                    hashed_password=get_password_hash(password),
                    role=role,
                    is_active=True,
                )
                session.add(new_user)
                logger.info("Seeded default user: %s with role: %s", username, role.value)

        await session.commit()
