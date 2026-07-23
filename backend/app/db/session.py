"""Database engine, session factory, and the FastAPI session dependency."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# sqlite connections are per-thread by default; FastAPI may hand a request's
# session to a different thread than the one that created it, so relax that
# check for sqlite only. Other backends (Postgres) don't need this.
_connect_args: dict[str, bool] = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=_connect_args)

SessionLocal = sessionmaker(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a Session, always closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
