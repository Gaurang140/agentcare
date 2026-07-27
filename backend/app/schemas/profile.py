"""Request/response schemas for the patient profile routes."""

from datetime import date
from typing import Literal

from pydantic import BaseModel


class ProfileOut(BaseModel):
    """The patient's own profile as the portal form reads it."""

    name: str
    email: str
    date_of_birth: date | None
    phone: str | None
    preferred_language: str
    emergency_contact: str | None


class ProfileUpdateRequest(BaseModel):
    """Partial update: only the provided fields change. The language is
    restricted to the two the agents actually answer in."""

    date_of_birth: date | None = None
    phone: str | None = None
    preferred_language: Literal["en", "de"] | None = None
    emergency_contact: str | None = None
