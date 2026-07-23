"""FastAPI application entrypoint: lifespan, middleware, routers, health."""

import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes_auth import router as auth_router
from app.api.routes_documents import router as documents_router
from app.api.routes_staff import router as staff_router
from app.config import settings
from app.db.session import get_db
from app.exceptions import register_exception_handlers
from app.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    yield


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
app.include_router(documents_router, prefix="/api")


@app.get("/api/health")
def health(db: Annotated[Session, Depends(get_db)]) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "db": True}
