"""Project exceptions and FastAPI error handling shared across service boundaries."""

from uuid import UUID, uuid4

import buzz
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from muninn.constants import ErrorKind


class MuninnError(buzz.Buzz):
    """Represent a project-owned failure."""

    kind = ErrorKind.REQUEST_FAILED
    status_code = 500
    safe_message = "Request failed"
    response_headers: dict[str, str] = {}


class ResourceNotFoundError(MuninnError):
    """Represent a resource unavailable to an API or MCP caller.

    Keep this shared because both HTTP route families use the same public
    status, kind, and safe-message contract.
    """

    kind = ErrorKind.RESOURCE_NOT_FOUND
    status_code = 404
    safe_message = "Resource not found"


class IdempotencyKeyReuseError(MuninnError):
    """Represent reuse of a key for a different request."""

    kind = ErrorKind.IDEMPOTENCY_KEY_REUSED
    status_code = 409
    safe_message = "Idempotency key was already used for a different request"

    def __init__(self, message: str = safe_message, *, base_message: str | None = None) -> None:
        super().__init__(message, base_message=base_message)


class ResourceIdConflictError(MuninnError):
    """Represent reuse of an existing caller-owned resource identity."""

    kind = ErrorKind.RESOURCE_ID_CONFLICT
    status_code = 409
    safe_message = "Resource ID is already in use"

    def __init__(self, message: str = safe_message) -> None:
        super().__init__(message)


def error_response(request: Request, kind: ErrorKind, message: str, status_code: int) -> JSONResponse:
    """Build the stable public error envelope."""
    from muninn.api.schemas import ErrorDetail, ErrorResponse

    request_id = request.headers.get("X-Request-ID", str(uuid4()))
    try:
        UUID(request_id)
    except ValueError:
        request_id = str(uuid4())
    body = ErrorResponse(error=ErrorDetail(kind=kind, message=message, request_id=UUID(request_id)))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers={"X-Request-ID": request_id}
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Install stable project, HTTP, and validation error handlers."""

    @app.exception_handler(MuninnError)
    async def muninn_error_handler(request: Request, exc: MuninnError) -> JSONResponse:
        """Translate a project error using its stable public classification."""
        response = error_response(request, exc.kind, exc.safe_message, exc.status_code)
        for key, value in exc.response_headers.items():
            response.headers[key] = value
        return response

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Translate FastAPI errors into the documented envelope."""
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        raw_kind = detail.get("kind", detail.get("code"))
        try:
            kind = ErrorKind(raw_kind)
        except (TypeError, ValueError):
            kind = ErrorKind.REQUEST_FAILED
        response = error_response(request, kind, str(detail.get("message", "Request failed")), exc.status_code)
        if exc.headers:
            for key, value in exc.headers.items():
                response.headers[key] = value
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, _: RequestValidationError) -> JSONResponse:
        """Return a stable validation error without echoing submitted content."""
        return error_response(request, ErrorKind.VALIDATION_ERROR, "Request validation failed", 422)
