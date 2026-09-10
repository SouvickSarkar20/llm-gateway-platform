"""Tenant, ApiKey, and TenantQuota database models."""

from datetime import datetime
import uuid
from typing import Optional
from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base


class Tenant(Base):
    """Customer company or organization entity."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    tier: Mapped[str] = mapped_column(
        String(32), default="free", index=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        # pyrefly: ignore [deprecated]
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        # pyrefly: ignore [deprecated]
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(
        "ApiKey", back_populates="tenant", cascade="all, delete-orphan"
    )
    quota: Mapped[Optional["TenantQuota"]] = relationship(
        "TenantQuota", back_populates="tenant", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant name={self.name} tier={self.tier}>"


class ApiKey(Base):
    """API Authentication Key assigned to a Tenant."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    key: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(
        String(64), default="Default Key", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        # pyrefly: ignore [deprecated]
        DateTime, default=datetime.utcnow, nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="api_keys")

    def __repr__(self) -> str:
        return f"<ApiKey key={self.key[:10]}... tenant_id={self.tenant_id}>"


class TenantQuota(Base):
    """Custom token limit and rate limit rules for a Tenant."""

    __tablename__ = "tenant_quotas"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tenants.id"), unique=True, index=True, nullable=False
    )
    max_monthly_tokens: Mapped[int] = mapped_column(
        Integer, default=1000000, nullable=False
    )
    current_monthly_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    rate_limit_rpm: Mapped[int] = mapped_column(
        Integer, default=60, nullable=False
    )
    burst_capacity: Mapped[int] = mapped_column(
        Integer, default=20, nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="quota")

    def __repr__(self) -> str:
        return f"<TenantQuota tenant_id={self.tenant_id} rpm={self.rate_limit_rpm}>"
