"""Alembic environment for forward-only service migrations."""

from alembic import context
from sqlalchemy import engine_from_config, pool

from muninn.config import service_settings
from muninn.db.models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""
    context.configure(url=context.config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database."""
    context.config.set_main_option("sqlalchemy.url", service_settings.database_url.replace("%", "%%"))
    connectable = engine_from_config(context.config.get_section(context.config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
