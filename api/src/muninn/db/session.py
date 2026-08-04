from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from muninn.config import service_settings


@lru_cache(maxsize=1)
def get_engine():
    """Create the process-wide database engine on first use."""
    return create_engine(service_settings.database_url, future=True)


@lru_cache(maxsize=1)
def get_session_maker():
    """Create the process-wide session maker on first use."""
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def session_factory(in_transaction: bool = False) -> Iterator[Session]:
    """Create and close a session, optionally wrapping its work in a transaction."""
    session: Session = get_session_maker()()
    try:
        if in_transaction:
            with session.begin():
                yield session
        else:
            yield session
    finally:
        session.close()


def db_session() -> Iterator[Session]:
    """Yield a database session for FastAPI dependencies."""
    with session_factory() as session:
        yield session
