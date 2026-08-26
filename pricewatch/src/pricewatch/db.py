"""Session plumbing. SQLite by default; PW_DB_URL can point at Postgres."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Callable, TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import settings

_engine = create_engine(
    settings.pw_db_url,
    future=True,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if settings.pw_db_url.startswith("sqlite") else {},
)

if settings.pw_db_url.startswith("sqlite"):
    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")      # concurrent reads while the scheduler writes
        cur.execute("PRAGMA busy_timeout=8000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
T = TypeVar("T")


def init_db() -> None:
    Base.metadata.create_all(_engine)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def in_db(fn: Callable[[Session], T]) -> T:
    """Run a blocking DB callable off the event loop."""
    def _run() -> T:
        with session_scope() as session:
            return fn(session)
    return await asyncio.to_thread(_run)
