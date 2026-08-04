"""Verify database session-factory transaction handling."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from muninn.db import session as db


@pytest.mark.unit
def test_session_factory_commits_and_rolls_back_transaction_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commit successful transaction scopes and roll back failed ones."""
    engine = create_engine("sqlite://")
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "get_session_maker", lambda: sessions)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE values_table (value INTEGER NOT NULL)"))

    with db.session_factory(in_transaction=True) as session:
        session.execute(text("INSERT INTO values_table (value) VALUES (1)"))

    with pytest.raises(RuntimeError, match="rollback"):
        with db.session_factory(in_transaction=True) as session:
            session.execute(text("INSERT INTO values_table (value) VALUES (2)"))
            raise RuntimeError("rollback")

    with sessions() as session:
        assert session.scalars(text("SELECT value FROM values_table ORDER BY value")).all() == [1]
    engine.dispose()


@pytest.mark.unit
def test_engine_is_created_lazily_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create the configured engine only when requested and reuse it thereafter."""
    db.get_engine.cache_clear()
    created_engines = []

    def create_engine(database_url: str, **kwargs: object):
        created_engines.append((database_url, kwargs))
        return object()

    monkeypatch.setattr(db, "create_engine", create_engine)

    assert created_engines == []
    first_engine = db.get_engine()
    second_engine = db.get_engine()

    assert first_engine is second_engine
    assert created_engines == [(db.service_settings.database_url, {"future": True})]
    db.get_engine.cache_clear()
