"""Construct and expose the Muninn FastAPI application."""

from fastapi import FastAPI

from muninn.api.routes.conversations import router as conversation_router
from muninn.api.routes.health import router as health_router
from muninn.exceptions import install_exception_handlers
from muninn.logging import init_logging
from muninn.middleware import install_middleware
from muninn.version import get_version

app = FastAPI(title="Muninn API", version=get_version())
init_logging()
install_middleware(app)
install_exception_handlers(app)
app.include_router(health_router)
app.include_router(conversation_router)
