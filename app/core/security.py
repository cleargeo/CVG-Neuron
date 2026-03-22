"""
CVG Neuron AI Orchestration System — Security & Authentication
Version: 2.0.0 | Clearview Geographic LLC

JWT-based authentication with role-based access control.
Supports: Bearer tokens, API keys, service-to-service tokens.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger("security")

# ── Password hashing ──────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── HTTP security schemes ─────────────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ── Built-in service API keys (for internal CVG system calls) ─────────────────
# Each internal service must have its OWN independent secret stored in the
# environment (CVG StratoVault / .env).  We NEVER derive service keys by
# slicing the user-facing JWT secret — that leaks the master secret if any
# service key is ever exposed.
import os as _os

def _load_service_key(env_var: str, fallback_name: str) -> str:
    """Load a service key from env; generate a stable fallback for dev only."""
    val = _os.environ.get(env_var, "")
    if val:
        return val
    # Dev-mode fallback: derive a HMAC of the secret with a fixed label so
    # different services still get different, non-overlapping keys.
    import hmac, hashlib
    return hmac.new(
        settings.secret_key.encode(),
        fallback_name.encode(),
        hashlib.sha256,
    ).hexdigest()

INTERNAL_SERVICE_KEYS: Dict[str, str] = {
    "CVG-HIVE":          _load_service_key("CVG_HIVE_SERVICE_KEY",          "cvg-hive"),
    "CVG-COMB":          _load_service_key("CVG_COMB_SERVICE_KEY",          "cvg-comb"),
    "CVG-OBSERVABILITY": _load_service_key("CVG_OBSERVABILITY_SERVICE_KEY", "cvg-observability"),
}


# ── JWT utilities ─────────────────────────────────────────────────────────────

def create_access_token(
    subject: str | Dict[str, Any],
    expires_delta: Optional[timedelta] = None,
    extra_claims: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Create a signed JWT access token.

    Args:
        subject: User ID or dict of claims to encode as `sub`
        expires_delta: Custom expiry (defaults to settings value)
        extra_claims: Additional claims to embed in the token

    Returns:
        Encoded JWT string
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)

    now = datetime.now(timezone.utc)
    expire = now + expires_delta

    payload: Dict[str, Any] = {
        "sub": str(subject) if not isinstance(subject, dict) else None,
        "iat": now,
        "exp": expire,
        "neuron_id": settings.neuron_id,
        "iss": "cvg-neuron",
    }

    if isinstance(subject, dict):
        payload.update(subject)

    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a JWT token.

    Raises:
        HTTPException(401) if token is invalid or expired
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
        )
        return payload
    except JWTError as exc:
        log.warning("JWT decode failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def create_service_token(service_name: str, ttl_hours: int = 24) -> str:
    """Create a long-lived service-to-service JWT."""
    return create_access_token(
        subject={"sub": service_name, "type": "service", "service": service_name},
        expires_delta=timedelta(hours=ttl_hours),
    )


# ── Password utilities ────────────────────────────────────────────────────────

def hash_password(plain_password: str) -> str:
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def generate_api_key() -> str:
    """Generate a cryptographically secure API key."""
    return f"cvg_{secrets.token_urlsafe(32)}"


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def get_current_token_payload(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key: Optional[str] = Security(api_key_header),
) -> Dict[str, Any]:
    """
    FastAPI dependency: extract & validate auth from Bearer token or API key.

    Returns decoded token payload dict.
    """
    # Try Bearer JWT first
    if credentials and credentials.scheme.lower() == "bearer":
        return decode_token(credentials.credentials)

    # Try API key
    if api_key:
        # Check internal service keys
        for service, key in INTERNAL_SERVICE_KEYS.items():
            if secrets.compare_digest(api_key, key):
                return {"sub": service, "type": "service", "service": service}

        # Check if it looks like a CVG API key (cvg_<token>)
        if api_key.startswith("cvg_"):
            # In production: validate against DB/StratoVault
            # For now, decode as JWT if applicable
            try:
                return decode_token(api_key[4:])
            except HTTPException:
                pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    payload: Dict[str, Any] = Depends(get_current_token_payload),
) -> Dict[str, Any]:
    """Dependency: returns current authenticated user/service info."""
    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing subject",
        )
    return payload


async def require_admin(
    payload: Dict[str, Any] = Depends(get_current_token_payload),
) -> Dict[str, Any]:
    """Dependency: requires admin role in token."""
    roles = payload.get("roles", [])
    if "admin" not in roles and payload.get("type") != "service":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return payload


# ── Optional auth (returns None if not provided) ──────────────────────────────

async def optional_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key: Optional[str] = Security(api_key_header),
) -> Optional[Dict[str, Any]]:
    """Dependency: returns token payload OR None — does not raise on missing auth."""
    try:
        return await get_current_token_payload(credentials, api_key)
    except HTTPException:
        return None
