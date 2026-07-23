"""Request/response schemas for the auth routes."""

from datetime import date

from pydantic import BaseModel


class RegisterRequest(BaseModel):
    """Patient self-registration payload: account + demographic profile."""

    name: str
    email: str
    password: str
    dob: date | None = None
    phone: str | None = None
    preferred_language: str = "en"
    emergency_contact: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserSummary(BaseModel):
    """Shape returned by register, login, and /me."""

    id: int
    name: str
    email: str
    role: str
