"""Application exception hierarchy and FastAPI exception handlers."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger

logger = get_logger(__name__)


class AppError(Exception):
    """Base application error. Carries an HTTP status code and a machine code."""

    status_code: int = 500
    code: str = "app_error"

    def __init__(self, message: str = "An application error occurred") -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class PermissionDeniedError(AppError):
    status_code = 403
    code = "permission_denied"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class SafetyBlockedError(AppError):
    status_code = 422
    code = "safety_blocked"


class ValidationError(AppError):
    """Bad client input that isn't a pydantic body-validation error, e.g. a
    rejected file upload (wrong extension, over the size cap)."""

    status_code = 400
    code = "validation_error"


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers that translate exceptions into JSON responses.

    AppError subclasses map to their declared status code and code.
    Any other unhandled exception is logged with a stack trace and reported
    to the client as a generic 500, without leaking internals.
    """

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(Exception)
    async def handle_unhandled_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error("unhandled_exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": "Something went wrong"},
        )
