"""API dependencies: database session, authentication, and RBAC guards."""

from typing import Callable, List, Optional
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User, UserRole

security_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and authenticate the current user from the Bearer JWT token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: Optional[str] = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token payload missing subject identifier",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fetch user from database
    stmt = select(User).where(User.id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user record no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated",
        )

    return user


def require_role(*allowed_roles: UserRole) -> Callable:
    """Factory creating an RBAC dependency that enforces role permissions."""

    async def role_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        if current_user.role not in allowed_roles:
            allowed_names = [r.value for r in allowed_roles]
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access forbidden: user role '{current_user.role.value}' "
                    f"does not have permission. Required role(s): {allowed_names}"
                ),
            )
        return current_user

    return role_checker


# Role-based dependency shortcuts
require_admin = require_role(UserRole.ADMIN)
require_user_or_admin = require_role(UserRole.USER, UserRole.ADMIN)
require_authenticated = get_current_user


def get_rate_limiter_guard(
    max_requests: Optional[int] = None,
    window_seconds: Optional[int] = None,
    default_tier: Optional[str] = None,
) -> Callable:
    """FastAPI dependency enforcing distributed Token Bucket rate limiting per tenant, API Key, user, or IP."""
    from fastapi import Request
    from app.services.rate_limiter import rate_limiter

    async def rate_limit_dependency(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Security(security_scheme),
    ) -> None:
        identifier = f"ip:{request.client.host if request.client else 'unknown'}"
        tenant_tier: Optional[str] = default_tier or request.headers.get("X-Tenant-Tier")

        # 1. Check X-API-Key header
        api_key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
        if api_key:
            identifier = f"tenant:{api_key}"
            if not tenant_tier:
                if api_key.startswith("sk-ent"):
                    tenant_tier = "enterprise"
                elif api_key.startswith("sk-pro"):
                    tenant_tier = "pro"
                elif api_key.startswith("sk-free"):
                    tenant_tier = "free"
        elif credentials and credentials.credentials:
            try:
                payload = decode_access_token(credentials.credentials)
                if sub := payload.get("sub"):
                    identifier = f"user:{sub}"
                    if role := payload.get("role"):
                        if role == "admin":
                            tenant_tier = tenant_tier or "enterprise"
                        elif role == "user":
                            tenant_tier = tenant_tier or "pro"
            except Exception:
                pass  # Fallback to IP identifier

        result = await rate_limiter.check_rate_limit(
            identifier=identifier,
            max_requests=max_requests,
            window_seconds=window_seconds,
            tenant_tier=tenant_tier,
        )

        if not result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: quota depleted for identifier '{identifier}'. Retry in {result.reset_in_seconds}s.",
                headers={
                    "Retry-After": str(result.reset_in_seconds),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(result.reset_in_seconds),
                },
            )

        # Store rate limit state on request for response headers
        request.state.rate_limit_result = result

    return rate_limit_dependency


def get_idempotency_key(request: Request) -> Optional[str]:
    """Extract Idempotency-Key or X-Idempotency-Key header from incoming request."""
    return (
        request.headers.get("Idempotency-Key")
        or request.headers.get("idempotency-key")
        or request.headers.get("X-Idempotency-Key")
        or request.headers.get("x-idempotency-key")
    )

