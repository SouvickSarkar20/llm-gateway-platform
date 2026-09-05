"""Authentication and authorization Pydantic schemas."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field
from app.models.user import UserRole


class LoginRequest(BaseModel):
    """Payload for POST /auth/login."""
    username: str = Field(..., min_length=3, max_length=64, description="Unique username or email")
    password: str = Field(..., min_length=6, description="Plaintext password")

    model_config = {
        "json_schema_extra": {
            "example": {
                "username": "user",
                "password": "user123",
            }
        }
    }


class TokenResponse(BaseModel):
    """JWT bearer token response."""
    access_token: str = Field(..., description="JWT bearer access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_in: int = Field(..., description="Expiration time in seconds")
    user_id: str = Field(..., description="User unique identifier")
    username: str = Field(..., description="Username")
    role: UserRole = Field(..., description="Assigned RBAC role")

    model_config = {
        "json_schema_extra": {
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "expires_in": 3600,
                "user_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                "username": "user",
                "role": "user",
            }
        }
    }


class UserResponse(BaseModel):
    """User profile response."""
    id: str
    username: str
    email: EmailStr
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {
            "example": {
                "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                "username": "user",
                "email": "user@platform.ai",
                "role": "user",
                "is_active": True,
                "created_at": "2026-09-05T12:00:00Z",
            }
        },
    }


class RegisterRequest(BaseModel):
    """Payload for user self-registration or admin user provisioning."""
    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr = Field(..., description="Valid corporate or personal email")
    password: str = Field(..., min_length=6, description="Account password")
    role: Optional[UserRole] = Field(default=UserRole.USER, description="Initial RBAC role")
