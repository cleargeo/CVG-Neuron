"""
CVG Neuron AI Orchestration System — /api/users Router
Version: 2.0.0 | Clearview Geographic LLC

User management and token generation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.core.security import (
    create_access_token,
    generate_api_key,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from app.models.response import NeuronResponse

router = APIRouter(prefix="/api/users", tags=["Users"])
log = get_logger("router.users")


# ── In-memory user store (replace with DB in production) ──────────────────────

class UserRecord(BaseModel):
    user_id: str
    username: str
    email: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    api_key: Optional[str] = None
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hashed_password: str = ""


_users: Dict[str, UserRecord] = {}


# ── Request/Response schemas ──────────────────────────────────────────────────

class UserCreate(BaseModel):
    username: str
    password: str
    email: Optional[str] = None
    roles: List[str] = Field(default_factory=lambda: ["user"])


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    username: str
    roles: List[str]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post(
    "/login",
    summary="Authenticate and get JWT token",
    response_model=NeuronResponse,
)
async def login(credentials: UserLogin) -> NeuronResponse:
    """
    Authenticate a user and return a JWT access token.

    **Default admin credentials (change in production!):**
    - username: `admin`
    - password: `cvg-neuron-admin`
    """
    # Seed default admin on first login attempt
    if not _users:
        _seed_default_admin()

    user = next((u for u in _users.values() if u.username == credentials.username), None)

    if not user or not verify_password(credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    if not user.active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    token = create_access_token(
        subject={
            "sub": user.user_id,
            "username": user.username,
            "roles": user.roles,
        }
    )

    from app.core.config import settings
    return NeuronResponse.ok(
        data=TokenResponse(
            access_token=token,
            expires_in=settings.access_token_expire_minutes * 60,
            user_id=user.user_id,
            username=user.username,
            roles=user.roles,
        ).model_dump(),
        message="Authentication successful",
    )


@router.get(
    "/me",
    response_model=NeuronResponse,
    summary="Get current user info",
)
async def get_me(
    auth: Dict[str, Any] = Depends(get_current_user),
) -> NeuronResponse:
    """Return information about the currently authenticated user."""
    user_id = auth.get("sub")
    user = _users.get(user_id)
    if user:
        return NeuronResponse.ok(
            data={
                "user_id": user.user_id,
                "username": user.username,
                "email": user.email,
                "roles": user.roles,
                "active": user.active,
                "created_at": user.created_at.isoformat(),
            },
            message="Current user",
        )
    # Service token
    return NeuronResponse.ok(
        data={"sub": auth.get("sub"), "type": auth.get("type", "user"), "roles": auth.get("roles", [])},
        message="Service token",
    )


@router.get(
    "",
    response_model=NeuronResponse,
    summary="List all users",
)
async def list_users(
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Return all registered users (admin only)."""
    if not _users:
        _seed_default_admin()

    users_out = [
        {
            "user_id": u.user_id,
            "username": u.username,
            "email": u.email,
            "roles": u.roles,
            "active": u.active,
            "has_api_key": bool(u.api_key),
            "created_at": u.created_at.isoformat(),
        }
        for u in _users.values()
    ]
    return NeuronResponse.ok(data=users_out, message=f"{len(users_out)} user(s)")


@router.post(
    "",
    response_model=NeuronResponse,
    summary="Create a new user",
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    user_data: UserCreate,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Create a new user account (admin only)."""
    # Check username uniqueness
    if any(u.username == user_data.username for u in _users.values()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{user_data.username}' already exists",
        )

    user = UserRecord(
        user_id=f"user-{uuid.uuid4().hex[:8]}",
        username=user_data.username,
        email=user_data.email,
        roles=user_data.roles,
        hashed_password=hash_password(user_data.password),
    )
    _users[user.user_id] = user
    log.info("User created", user_id=user.user_id, username=user.username)

    return NeuronResponse.ok(
        data={"user_id": user.user_id, "username": user.username, "roles": user.roles},
        message=f"User '{user.username}' created",
    )


@router.post(
    "/{user_id}/api-key",
    response_model=NeuronResponse,
    summary="Generate API key for user",
)
async def generate_user_api_key(
    user_id: str,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """Generate a new API key for a user."""
    user = _users.get(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    api_key = generate_api_key()
    user.api_key = api_key  # In production, store hashed
    log.info("API key generated", user_id=user_id)

    return NeuronResponse.ok(
        data={"api_key": api_key, "user_id": user_id},
        message="API key generated — store this securely, it will not be shown again",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_default_admin() -> None:
    """Create the default admin account on first use."""
    admin_id = "user-admin-default"
    _users[admin_id] = UserRecord(
        user_id=admin_id,
        username="admin",
        email="admin@cleargeo.tech",
        roles=["admin", "user"],
        hashed_password=hash_password("cvg-neuron-admin"),
    )
    log.warning(
        "Default admin account created — CHANGE PASSWORD IN PRODUCTION",
        username="admin",
    )
