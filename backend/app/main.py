"""FastAPI application entrypoint: lifespan, middleware, routers, health."""

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.routes_auth import router as auth_router
from app.api.routes_documents import router as documents_router
from app.api.routes_events import router as events_router
from app.api.routes_patient import router as patient_router
from app.api.routes_staff import internal_router, router as staff_router
from app.api.routes_workflows import requests_router, router as workflows_router
from app.config import settings
from app.db.session import get_db
from app.exceptions import register_exception_handlers
from app.logging_setup import configure_logging, get_logger
from app.observability.tracing import shutdown_tracing
from app.scheduler import start_scheduler, stop_scheduler
from app.services import workflow_service

logger = get_logger(__name__)
EXPECTED_DATABASE_REVISION = "c1d2e3f4a5b6"


def _require_current_database_revision(
    revision: str | None,
    environment: str,
) -> str:
    if environment.strip().lower() in {"dev", "development", "test"}:
        return revision or "unmanaged"
    if revision != EXPECTED_DATABASE_REVISION:
        raise RuntimeError(
            "database revision does not match this application release"
        )
    return revision


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    # Builds and compiles the workflow graph once, opening its checkpointer
    # for the app's lifetime; app.state.graph is the same singleton
    # app.services.workflow_service uses internally, not a second instance.
    app.state.graph = workflow_service.get_graph()
    # No-op under TESTING (see app/scheduler.py) - never double-starts.
    app.state.scheduler = start_scheduler()
    yield
    stop_scheduler()
    workflow_service.close_graph()
    shutdown_tracing()


app = FastAPI(title="AgentCare", lifespan=lifespan)

# allow_credentials=True forbids wildcard origins/methods/headers: everything
# below is an explicit list, per the verified stack notes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

register_exception_handlers(app)

Instrumentator().instrument(app).expose(app)


@app.middleware("http")
async def request_id_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["X-Request-Id"] = request_id
    return response


app.include_router(auth_router, prefix="/api")
app.include_router(staff_router, prefix="/api")
app.include_router(internal_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(requests_router, prefix="/api")
app.include_router(workflows_router, prefix="/api")
app.include_router(events_router, prefix="/api")
app.include_router(patient_router, prefix="/api")


@app.get("/api/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health")
def health(db: Annotated[Session, Depends(get_db)]) -> dict:
    db.execute(text("SELECT 1"))
    dialect = db.get_bind().dialect.name
    try:
        revision = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
    except SQLAlchemyError:
        if settings.environment.strip().lower() not in {
            "dev",
            "development",
            "test",
        }:
            raise
        revision = "unmanaged"
    revision = _require_current_database_revision(
        revision,
        settings.environment,
    )
    return {
        "status": "ok",
        "db": True,
        "database_dialect": dialect,
        "database_revision": revision,
        "release": settings.app_release or "dev",
    }
