from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings

try:
    from pgvector.psycopg import register_vector
except ImportError:  # pragma: no cover - optional import for sqlite-only test environments
    register_vector = None


def create_engine_from_settings(settings: Settings):
    kwargs = {"future": True, "pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(settings.database_url, **kwargs)
    if settings.database_url.startswith("postgresql") and register_vector is not None:
        @event.listens_for(engine, "connect")
        def _register_pgvector(dbapi_connection, connection_record):  # pragma: no cover - exercised in postgres deployments
            register_vector(dbapi_connection)
    return engine


def create_session_maker(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
