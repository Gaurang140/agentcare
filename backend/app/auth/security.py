"""Password hashing (pwdlib, Argon2id) and JWT helpers (PyJWT, HS256).

passlib is dead for this project; pwdlib's PasswordHash.recommended() picks
Argon2id, per the verified stack notes. algorithms=["HS256"] is hardcoded on
decode, never taken from the token header.
"""

from datetime import datetime, timedelta, timezone

import jwt

from pwdlib import PasswordHash

from app.config import settings

_password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored Argon2id hash."""
    return _password_hash.verify(password, password_hash)


def create_token(user_id: int, role: str) -> str:
    """Encode an HS256 JWT carrying the user id and role, expiring per settings."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    """Decode and verify an HS256 JWT.

    Raises jwt.exceptions.InvalidTokenError (or a subclass) on any failure:
    bad signature, malformed token, or expiry. Callers translate that into
    the app's PermissionDeniedError.
    """
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
