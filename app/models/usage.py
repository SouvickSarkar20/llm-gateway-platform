"""LLM request usage and cost tracking model."""

from datetime import datetime
import enum
import uuid
from typing import Optional
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Enum, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class RequestStatus(str, enum.Enum):
    """Execution status of an LLM request."""
    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
    FAILED = "FAILED"
    CACHE_HIT = "CACHE_HIT"


class LLMUsageLog(Base):
    """Persistent audit log tracking every LLM request, token count, cost, and latency."""

    __tablename__ = "llm_usage_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), index=True, nullable=False
    )
    tenant_id: Mapped[Optional[str]] = mapped_column(
        String(36), index=True, nullable=True
    )
    model: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False
    )
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    estimated_cost_usd: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    latency_ms: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    cache_hit: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    status: Mapped[RequestStatus] = mapped_column(
        Enum(RequestStatus), default=RequestStatus.SUCCESS, nullable=False
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        # pyrefly: ignore [deprecated]
        DateTime, default=datetime.utcnow, index=True, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<LLMUsageLog id={self.id} user={self.user_id} model={self.model} "
            f"tokens={self.total_tokens} cost=${self.estimated_cost_usd:.6f} "
            f"latency={self.latency_ms:.1f}ms status={self.status}>"
        )
