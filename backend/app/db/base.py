"""Declarative base shared by every ORM model.

Every model in app/models/ subclasses this Base so that a single
Base.metadata carries the full schema for Alembic autogenerate and for
Base.metadata.create_all() in tests.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all AgentCare ORM models."""
