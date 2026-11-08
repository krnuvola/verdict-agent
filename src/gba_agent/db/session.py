"""Database engine and session lifecycle helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from gba_agent.config import Settings


def create_database_engine(settings: Settings, *, echo: bool = False) -> Engine:
    """Create the PostgreSQL engine without exposing the configured URL."""
    return create_engine(
        settings.database_url.get_secret_value(),
        echo=echo,
        pool_pre_ping=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a typed SQLAlchemy session factory."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session and commit on success or roll back on failure."""
    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


def verify_database(engine: Engine, *, require_migrated: bool = True) -> None:
    """Fail startup if PostgreSQL is unreachable or migrations are missing."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        if require_migrated:
            table_names = inspect(connection).get_table_names()
            if "alembic_version" not in table_names:
                raise RuntimeError("agentdb has not been migrated; run `alembic upgrade head`")
