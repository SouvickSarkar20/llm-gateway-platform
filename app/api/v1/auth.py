"""Authentication and authorization API endpoints."""

from datetime import timedelta
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from app.config import get_settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    get_password_hash,
    verify_password,
)
from app.models.user import User, UserRole
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.api.deps import get_current_user, require_admin

logger = logging.getLogger("chat_platform.auth")
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["Authentication & RBAC"])


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate user and issue JWT bearer token",
    status_code=status.HTTP_200_OK,
)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Validate credentials and return signed JWT with subject ID and RBAC role."""
    # Look up user by username or email
    stmt = select(User).where(
        or_(User.username == payload.username, User.email == payload.username)
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.hashed_password):
        logger.warning("Failed login attempt for username: %s", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive. Please contact your platform administrator.",
        )

    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(
        subject=user.id,
        role=user.role.value,
        extra_claims={"username": user.username},
        expires_delta=expires_delta,
    )

    logger.info("User '%s' (role: %s) logged in successfully", user.username, user.role.value)

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user_id=user.id,
        username=user.username,
        role=user.role,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get profile of currently authenticated user",
)
async def get_me(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Retrieve profile and RBAC role details of the current bearer token subject."""
    return UserResponse.model_validate(current_user)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def register(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Create a new user account with secure password hashing."""
    # Check for existing user
    stmt = select(User).where(
        or_(User.username == payload.username, User.email == payload.email)
    )
    result = await db.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this username or email already exists",
        )

    # By default, open registration only creates USER role; ADMIN cannot be self-assigned
    role = UserRole.USER if payload.role != UserRole.ADMIN else UserRole.USER

    new_user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
        role=role,
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    logger.info("Registered new user: %s with role: %s", new_user.username, new_user.role.value)
    return UserResponse.model_validate(new_user)
