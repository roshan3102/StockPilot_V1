"""Database engine and schema helpers.

An *Engine* owns the connections to one database. A *Session* (created from an engine) is
a unit of work: you add/change objects, then `commit()` writes everything atomically or
`rollback()` discards it. Services receive a Session; they never create their own.
"""

from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import get_settings
from app.models import Base


def make_engine(url: str) -> Engine:
    """Create an engine. Tests pass `sqlite://` (a private in-memory database)."""
    engine = create_engine(url)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection: DBAPIConnection, _: ConnectionPoolEntry):
            # SQLite ships with foreign-key enforcement OFF for backwards compatibility,
            # and the setting is per-connection. Without this, every FOREIGN KEY in
            # models.py would be decoration. This hook runs on each new connection.
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


@lru_cache
def get_engine() -> Engine:
    """The app's engine, built once from DATABASE_URL."""
    return make_engine(get_settings().database_url)


def reset_schema(engine: Engine) -> None:
    """Drop every table and recreate it empty. Destroys all data (D7: no Alembic)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
