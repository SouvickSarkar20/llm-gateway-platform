"""Resilient LLM Gateway with Circuit Breaker, Exponential Backoff + Jitter, and Fallback."""

import abc
import asyncio
import enum
import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple
import httpx

from app.config import get_settings

logger = logging.getLogger("chat_platform.llm_gateway")
settings = get_settings()


# ==========================================
# 1. Error Classification & Exceptions
# ==========================================

class LLMGatewayException(Exception):
    """Base exception for LLM Gateway failures."""
    def __init__(self, message: str, status_code: int = 500, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


class LLMTimeoutException(LLMGatewayException):
    """Network or execution timeout while contacting LLM provider."""
    def __init__(self, message: str = "LLM request timed out"):
        super().__init__(message, status_code=504, retryable=True)


class LLMRateLimitException(LLMGatewayException):
    """Upstream LLM provider returned HTTP 429 rate limit."""
    def __init__(self, message: str = "LLM provider rate limit exceeded (HTTP 429)"):
        super().__init__(message, status_code=429, retryable=True)


class LLMServerException(LLMGatewayException):
    """Upstream LLM provider returned 5xx server error."""
    def __init__(self, message: str = "LLM provider internal error (5xx)", status_code: int = 503):
        super().__init__(message, status_code=status_code, retryable=True)


class LLMClientException(LLMGatewayException):
    """Non-retryable client error (4xx bad prompt, invalid key, etc.)."""
    def __init__(self, message: str = "Invalid request or prompt error (4xx)", status_code: int = 400):
        super().__init__(message, status_code=status_code, retryable=False)


class CircuitBreakerOpenException(LLMGatewayException):
    """Tripped circuit breaker fast-fails before network call."""
    def __init__(self, provider: str, recovery_time_remaining: float):
        message = (
            f"Circuit breaker for provider '{provider}' is OPEN. "
            f"Fast-failing request. Recovery in {recovery_time_remaining:.1f}s."
        )
        super().__init__(message, status_code=503, retryable=False)
        self.provider = provider
        self.recovery_time_remaining = recovery_time_remaining


def is_retryable_error(exc: Exception) -> bool:
    """Classify whether an exception should be retried with backoff."""
    if isinstance(exc, (LLMTimeoutException, LLMRateLimitException, LLMServerException)):
        return True
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 429 and 5xx are retryable; 4xx are client errors and non-retryable
        return status == 429 or status >= 500
    if isinstance(exc, LLMGatewayException):
        return exc.retryable
    return False


# ==========================================
# 2. Circuit Breaker (3-State Machine)
# ==========================================

class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"        # Normal operation: traffic flows freely
    OPEN = "OPEN"            # Provider degraded: requests fail-fast or route to fallback
    HALF_OPEN = "HALF_OPEN"  # Testing recovery: single probe requests allowed


class CircuitBreaker:
    """Production 3-State Circuit Breaker preventing cascading upstream failures."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def can_execute(self) -> Tuple[bool, float]:
        """Check if request is permitted to proceed through the circuit."""
        async with self._lock:
            now = time.monotonic()
            if self._state == CircuitState.CLOSED:
                return True, 0.0

            if self._state == CircuitState.OPEN:
                elapsed = now - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._consecutive_successes = 0
                    logger.info(
                        "Circuit breaker '%s' transitioned from OPEN to HALF_OPEN (probing)",
                        self.name,
                    )
                    return True, 0.0
                remaining = self.recovery_timeout - elapsed
                return False, remaining

            if self._state == CircuitState.HALF_OPEN:
                # In half-open state, allow probe requests
                return True, 0.0

            return False, 0.0

    async def record_success(self) -> None:
        """Record successful execution."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.half_open_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    logger.info(
                        "Circuit breaker '%s' recovered! Transitioned from HALF_OPEN to CLOSED",
                        self.name,
                    )
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    async def record_failure(self) -> None:
        """Record failure and trip circuit if threshold exceeded."""
        async with self._lock:
            self._last_failure_time = time.monotonic()
            self._consecutive_failures += 1

            if self._state == CircuitState.HALF_OPEN:
                # Any failure during half-open trips back to OPEN immediately
                self._state = CircuitState.OPEN
                logger.warning(
                    "Circuit breaker '%s' probe failed! Tripped back to OPEN",
                    self.name,
                )
            elif (
                self._state == CircuitState.CLOSED
                and self._consecutive_failures >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                logger.error(
                    "Circuit breaker '%s' tripped to OPEN! Reached %d consecutive failures",
                    self.name,
                    self._consecutive_failures,
                )

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic state for monitoring and metrics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "consecutive_successes": self._consecutive_successes,
            "last_failure_time": self._last_failure_time,
        }


# ==========================================
# 3. Request & Response Schemas
# ==========================================

class LLMMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMRequest:
    def __init__(
        self,
        messages: List[LLMMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ):
        self.messages = messages
        self.model = model or settings.PRIMARY_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens


class LLMResponse:
    def __init__(
        self,
        content: str,
        model_used: str,
        provider: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: float,
        is_fallback: bool = False,
        cached: bool = False,
    ):
        self.content = content
        self.model_used = model_used
        self.provider = provider
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        self.latency_ms = latency_ms
        self.is_fallback = is_fallback
        self.cached = cached

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "model_used": self.model_used,
            "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "is_fallback": self.is_fallback,
            "cached": self.cached,
        }


# ==========================================
# 4. Provider Implementations
# ==========================================

class BaseLLMProvider(abc.ABC):
    """Abstract interface for LLM providers."""

    @abc.abstractmethod
    async def call(self, request: LLMRequest) -> LLMResponse:
        """Call provider endpoint and return unified LLMResponse."""
        pass


class MockLLMProvider(BaseLLMProvider):
    """Deterministic Mock Provider for unit testing, offline development, and fault injection."""

    def __init__(self):
        # Fault injection simulation controls
        self.fail_mode: Optional[str] = None  # "timeout", "rate_limit", "server_error", "client_error"
        self.fail_count: int = 0
        self.simulated_delay: float = 0.05

    def set_fault(self, mode: Optional[str], count: int = 1) -> None:
        """Inject simulated faults for testing resilience."""
        self.fail_mode = mode
        self.fail_count = count

    async def call(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()

        if self.simulated_delay > 0:
            await asyncio.sleep(self.simulated_delay)

        if self.fail_count > 0 and self.fail_mode:
            self.fail_count -= 1
            if self.fail_mode == "timeout":
                raise LLMTimeoutException("Simulated upstream provider timeout")
            elif self.fail_mode == "rate_limit":
                raise LLMRateLimitException("Simulated upstream HTTP 429 rate limit")
            elif self.fail_mode == "server_error":
                raise LLMServerException("Simulated upstream HTTP 503 service unavailable")
            elif self.fail_mode == "client_error":
                raise LLMClientException("Simulated upstream HTTP 400 bad prompt")

        # Generate realistic mock response
        last_msg = request.messages[-1].content if request.messages else "Hello"
        content = (
            f"[AI Response from {request.model}]: "
            f"Regarding your query '{last_msg[:60]}...', "
            f"here is a structured, production-ready analysis."
        )

        prompt_chars = sum(len(m.content) for m in request.messages)
        prompt_tokens = max(1, prompt_chars // 4)
        completion_tokens = max(1, len(content) // 4)
        total_tokens = prompt_tokens + completion_tokens
        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return LLMResponse(
            content=content,
            model_used=request.model,
            provider="mock",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            is_fallback=False,
        )


class OpenAILLMProvider(BaseLLMProvider):
    """OpenAI API Provider using async HTTP client."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = "https://api.openai.com/v1/chat/completions"

    async def call(self, request: LLMRequest) -> LLMResponse:
        if not self.api_key:
            raise LLMClientException("OPENAI_API_KEY is not configured", status_code=500)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": request.model,
            "messages": [m.to_dict() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        start_time = time.perf_counter()
        async with httpx.AsyncClient(timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS) as client:
            try:
                res = await client.post(self.base_url, headers=headers, json=payload)
                res.raise_for_status()
                data = res.json()
            except httpx.TimeoutException as exc:
                raise LLMTimeoutException(f"OpenAI request timed out: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429:
                    raise LLMRateLimitException(f"OpenAI rate limit: {exc.response.text}") from exc
                elif code >= 500:
                    raise LLMServerException(f"OpenAI server error: {code}", status_code=code) from exc
                else:
                    raise LLMClientException(f"OpenAI client error: {exc.response.text}", status_code=code) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            model_used=request.model,
            provider="openai",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=round(latency_ms, 2),
            is_fallback=False,
        )


class GeminiLLMProvider(BaseLLMProvider):
    """Google Gemini API Provider using async HTTP client."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def call(self, request: LLMRequest) -> LLMResponse:
        key = self.api_key or settings.GEMINI_API_KEY
        if not key:
            raise LLMClientException("GEMINI_API_KEY is not configured", status_code=500)

        # Normalize model name
        model_name = request.model if "gemini" in request.model.lower() else "gemini-1.5-flash"
        url = f"{self.base_url}/{model_name}:generateContent?key={key}"

        contents = []
        for m in request.messages:
            role = "user" if m.role in ("user", "system") else "model"
            contents.append({"role": role, "parts": [{"text": m.content}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }

        start_time = time.perf_counter()
        async with httpx.AsyncClient(timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS) as client:
            try:
                res = await client.post(url, json=payload)
                res.raise_for_status()
                data = res.json()
            except httpx.TimeoutException as exc:
                raise LLMTimeoutException(f"Gemini request timed out: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code == 429:
                    raise LLMRateLimitException(f"Gemini rate limit: {exc.response.text}") from exc
                elif code >= 500:
                    raise LLMServerException(f"Gemini server error: {code}", status_code=code) from exc
                else:
                    raise LLMClientException(f"Gemini client error: {exc.response.text}", status_code=code) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        try:
            content = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMServerException(f"Malformed response from Gemini: {data}") from exc

        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        completion_tokens = usage.get("candidatesTokenCount", 0)
        total_tokens = usage.get("totalTokenCount", prompt_tokens + completion_tokens)

        return LLMResponse(
            content=content,
            model_used=model_name,
            provider="gemini",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=round(latency_ms, 2),
            is_fallback=False,
        )


# ==========================================
# 5. Resilient LLM Gateway (Orchestrator)
# ==========================================

class ResilientLLMGateway:
    """Enterprise LLM Gateway orchestrating retries, circuit breaking, and fallbacks."""

    def __init__(self):
        self.primary_provider_name = settings.LLM_PROVIDER
        self.primary_circuit = CircuitBreaker(
            name="primary_llm",
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
        )
        self.fallback_circuit = CircuitBreaker(
            name="fallback_llm",
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            recovery_timeout=settings.CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS,
        )

        # Provider instances
        self._providers: Dict[str, BaseLLMProvider] = {
            "mock": MockLLMProvider(),
            "openai": OpenAILLMProvider(),
            "gemini": GeminiLLMProvider(),
        }

    def get_provider(self, name: str) -> BaseLLMProvider:
        """Retrieve provider implementation."""
        if name not in self._providers:
            # Default to mock provider if unknown
            return self._providers["mock"]
        return self._providers[name]

    def _calculate_backoff(self, attempt: int) -> float:
        """Compute exponential backoff with full randomized jitter."""
        # sleep = min(max_backoff, base * 2^attempt) + jitter
        exp_backoff = min(
            settings.MAX_BACKOFF_SECONDS,
            settings.BASE_BACKOFF_SECONDS * (2 ** attempt),
        )
        jitter = random.uniform(0, exp_backoff * settings.JITTER_FACTOR)
        return exp_backoff + jitter

    async def _execute_with_retry(
        self,
        provider: BaseLLMProvider,
        circuit: CircuitBreaker,
        request: LLMRequest,
    ) -> LLMResponse:
        """Execute request against provider with Circuit Breaker and Exponential Backoff."""
        # 1. Circuit Breaker check
        can_run, remaining = await circuit.can_execute()
        if not can_run:
            logger.warning(
                "Circuit breaker '%s' is OPEN. Fast-failing attempt (recovery remaining: %.1fs)",
                circuit.name,
                remaining,
            )
            raise CircuitBreakerOpenException(circuit.name, remaining)

        last_exception: Optional[Exception] = None

        # 2. Retry loop with backoff + jitter
        for attempt in range(settings.MAX_RETRIES + 1):
            try:
                response = await provider.call(request)
                # Success: notify circuit breaker
                await circuit.record_success()
                return response
            except Exception as exc:
                last_exception = exc
                await circuit.record_failure()

                if not is_retryable_error(exc):
                    logger.error(
                        "Non-retryable error encountered: %s. Failing fast.", exc
                    )
                    raise exc

                if attempt < settings.MAX_RETRIES:
                    backoff_sec = self._calculate_backoff(attempt)
                    logger.warning(
                        "Attempt %d/%d failed with retryable error: %s. "
                        "Backing off for %.2fs before retry...",
                        attempt + 1,
                        settings.MAX_RETRIES,
                        exc,
                        backoff_sec,
                    )
                    await asyncio.sleep(backoff_sec)
                else:
                    logger.error(
                        "Exhausted all %d retries for provider '%s': %s",
                        settings.MAX_RETRIES,
                        circuit.name,
                        exc,
                    )

        raise last_exception or LLMGatewayException("Unknown execution failure")

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Top-level entrypoint: attempts primary model; falls back to secondary model upon failure."""
        primary_provider = self.get_provider(self.primary_provider_name)

        # 1. Attempt Primary Provider & Model
        try:
            primary_request = LLMRequest(
                messages=request.messages,
                model=settings.PRIMARY_MODEL,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            response = await self._execute_with_retry(
                provider=primary_provider,
                circuit=self.primary_circuit,
                request=primary_request,
            )
            response.is_fallback = False
            return response

        except (CircuitBreakerOpenException, LLMTimeoutException, LLMRateLimitException, LLMServerException) as exc:
            logger.warning(
                "Primary model '%s' failed or circuit open (%s). Initiating fallback to '%s'...",
                settings.PRIMARY_MODEL,
                exc,
                settings.FALLBACK_MODEL,
            )

        except LLMClientException:
            # Client errors (e.g. 400, bad prompt) should NOT fall back to another model
            raise

        # 2. Model Fallback Execution
        try:
            fallback_request = LLMRequest(
                messages=request.messages,
                model=settings.FALLBACK_MODEL,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            fallback_response = await self._execute_with_retry(
                provider=primary_provider,
                circuit=self.fallback_circuit,
                request=fallback_request,
            )
            fallback_response.is_fallback = True
            logger.info(
                "Fallback model '%s' completed request successfully.",
                settings.FALLBACK_MODEL,
            )
            return fallback_response

        except Exception as fallback_exc:
            logger.critical(
                "Both primary model '%s' and fallback model '%s' failed. System degraded. Error: %s",
                settings.PRIMARY_MODEL,
                settings.FALLBACK_MODEL,
                fallback_exc,
            )
            raise LLMGatewayException(
                message=f"All available LLM providers and fallbacks failed: {str(fallback_exc)}",
                status_code=503,
                retryable=True,
            ) from fallback_exc


# Global gateway singleton
gateway = ResilientLLMGateway()
