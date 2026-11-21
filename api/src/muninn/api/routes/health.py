"""Health and readiness endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from muninn.api.exceptions import DatabaseNotReadyError
from muninn.db import db_session

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Report process liveness only."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(db: Session = Depends(db_session)) -> dict[str, str]:
    """Report database readiness without returning conversation content."""
    with DatabaseNotReadyError.handle_errors("Database readiness check failed"):
        db.execute(text("SELECT 1"))
    return {"status": "ready"}
