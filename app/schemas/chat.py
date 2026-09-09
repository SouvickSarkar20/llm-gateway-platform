"""Pydantic schemas for /chat endpoint and usage analytics."""

from typing import List, Optional
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """Single conversational turn message."""
    role: str = Field(..., description="Role: 'user', 'assistant', or 'system'")
    content: str = Field(..., min_length=1, description="Message content")


class ChatRequest(BaseModel):
    """Payload for POST /chat."""
    question: Optional[str] = Field(
        None,
        description="The primary user question (required if messages is omitted)",
        min_length=1,
    )
    messages: Optional[List[ChatMessage]] = Field(
        None,
        description="Optional list of multi-turn conversational messages",
    )
    model: Optional[str] = Field(
        None,
        description="Optional model override (e.g. 'gpt-4o-mini')",
    )
    temperature: Optional[float] = Field(
        0.7,
        ge=0.0,
        le=2.0,
        description="Sampling temperature",
    )
    max_tokens: Optional[int] = Field(
        1024,
        ge=1,
        le=4096,
        description="Maximum tokens to generate",
    )
    use_cache: bool = Field(
        True,
        description="Whether to check and store results in the Redis cache-aside layer",
    )
    async_mode: bool = Field(
        False,
        description="Explicitly offload request processing to async priority queue returning HTTP 202",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "question": "What is the CAP theorem and how does it relate to distributed databases?",
                "model": "gpt-4o-mini",
                "temperature": 0.7,
                "use_cache": True,
            }
        }
    }


class UsageStats(BaseModel):
    """Token consumption, cost, and latency metrics."""
    prompt_tokens: int = Field(..., description="Tokens in user prompt")
    completion_tokens: int = Field(..., description="Tokens in generated response")
    total_tokens: int = Field(..., description="Total tokens consumed")
    estimated_cost_usd: float = Field(..., description="Calculated cost in USD")
    latency_ms: float = Field(..., description="Round-trip execution latency in milliseconds")


class ChatResponse(BaseModel):
    """Response returned by POST /chat."""
    answer: str = Field(..., description="The LLM-generated answer")
    model_used: str = Field(..., description="Specific model that produced the answer")
    provider: str = Field(..., description="Underlying provider (mock, openai, etc.)")
    is_fallback: bool = Field(..., description="True if primary model failed and fallback was used")
    cache_hit: bool = Field(..., description="True if response was served from Redis cache")
    usage: UsageStats = Field(..., description="Token, cost, and latency analytics")
    request_id: str = Field(..., description="Correlation request identifier")

    model_config = {
        "protected_namespaces": (),
        "json_schema_extra": {
            "example": {
                "answer": "The CAP theorem states that a distributed data store can only provide two of three guarantees: Consistency, Availability, and Partition tolerance...",
                "model_used": "gpt-4o-mini",
                "provider": "mock",
                "is_fallback": False,
                "cache_hit": False,
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 45,
                    "total_tokens": 65,
                    "estimated_cost_usd": 0.000030,
                    "latency_ms": 115.4,
                },
                "request_id": "c1f7b0f6-d189-4e78-a28a-78a02c918a91",
            }
        }
    }


class AsyncJobResponse(BaseModel):
    """HTTP 202 Accepted response for queued asynchronous LLM requests."""
    job_id: str = Field(..., description="Unique tracking ID for the queued job")
    status: str = Field("queued", description="Initial status of the job (queued)")
    message: str = Field(..., description="Informational message regarding queueing and backpressure")
    poll_url: str = Field(..., description="Endpoint URL to poll job status and result")
    stream_url: str = Field(..., description="Server-Sent Events (SSE) streaming URL for live progress updates")

    model_config = {
        "protected_namespaces": (),
    }
