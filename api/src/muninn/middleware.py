from uuid import UUID, uuid4

from fastapi import FastAPI, Request


def install_middleware(app: FastAPI) -> None:
    """Install app middleware."""

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        """Attach a normalized correlation ID without logging conversation content."""
        identifier = request.headers.get("X-Request-ID")
        try:
            UUID(identifier or "")
        except ValueError:
            identifier = str(uuid4())
        request.state.request_id = identifier
        response = await call_next(request)
        response.headers["X-Request-ID"] = identifier
        return response
