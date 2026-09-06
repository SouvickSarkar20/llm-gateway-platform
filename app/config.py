"""Application configuration module using Pydantic Settings."""

from functools import lru_cache
from typing import Dict, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Production configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core Application Settings
    APP_NAME: str = "AI-Platform-QA-API"
    APP_ENV: str = "development"
    DEBUG: bool = False
    PORT: int = 8000
    HOST: str = "0.0.0.0"
    API_V1_PREFIX: str = "/api/v1"

    # Security & JWT Authentication
    JWT_SECRET_KEY: str = "dev_jwt_secret_key_change_in_production_32chars"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Database Configuration
    DATABASE_URL: str = "sqlite+aiosqlite:///./chat_platform.db"
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 10
    DB_ECHO: bool = False

    # Redis Configuration
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL_SECONDS: int = 3600
    REDIS_CONNECT_TIMEOUT_SECONDS: float = 2.0

    # Rate Limiting Configuration
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = 60
    RATE_LIMIT_BURST: int = 10

    # LLM Gateway Configuration
    LLM_PROVIDER: str = "mock"  # "mock", "openai", "gemini"
    PRIMARY_MODEL: str = "gpt-4o-mini"
    FALLBACK_MODEL: str = "gpt-3.5-turbo"
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    LLM_REQUEST_TIMEOUT_SECONDS: float = 35.0

    # Circuit Breaker & Retry Settings
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS: float = 30.0
    MAX_RETRIES: int = 3
    BASE_BACKOFF_SECONDS: float = 0.5
    MAX_BACKOFF_SECONDS: float = 4.0
    JITTER_FACTOR: float = 0.25

    # Observability
    PROMETHEUS_METRICS_PATH: str = "/metrics"
    ENABLE_STRUCTURED_LOGGING: bool = True

    # Pricing per 1k tokens in USD for cost tracking
    # [prompt_cost_per_1k, completion_cost_per_1k]
    MODEL_PRICING: Dict[str, Dict[str, float]] = Field(
        default_factory=lambda: {
            "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.00060},
            "gpt-3.5-turbo": {"prompt": 0.00050, "completion": 0.00150},
            "gemini-1.5-flash": {"prompt": 0.000075, "completion": 0.00030},
            "mock-model": {"prompt": 0.00010, "completion": 0.00020},
        }
    )

    def calculate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Calculate estimated cost in USD based on model pricing table."""
        pricing = self.MODEL_PRICING.get(
            model, {"prompt": 0.00015, "completion": 0.00060}
        )
        prompt_cost = (prompt_tokens / 1000.0) * pricing["prompt"]
        completion_cost = (completion_tokens / 1000.0) * pricing["completion"]
        return round(prompt_cost + completion_cost, 7)


@lru_cache()
def get_settings() -> Settings:
    """Cached singleton settings instance."""
    return Settings()
