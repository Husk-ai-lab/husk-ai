from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from husk_studio_backend.config import db_url, sync_db_url
from husk_studio_shared import RECORDING_FORMAT_VERSION, RecordingFormatError, check_format_version

_async_engine: AsyncEngine | None = None
_sync_engine: Engine | None = None
_async_factory: async_sessionmaker[AsyncSession] | None = None
_sync_factory: sessionmaker[Session] | None = None


def _apply_pragmas(engine: Engine) -> None:
    """SQLite: WAL + foreign keys + reasonable synchronous level."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def async_engine() -> AsyncEngine:
    global _async_engine, _async_factory
    if _async_engine is None:
        _async_engine = create_async_engine(db_url(), echo=False, future=True)
        if _async_engine.dialect.name == "sqlite":
            # The pragmas have to be attached to the underlying sync Engine — an
            # AsyncEngine proxies the connection pool but does not emit its own
            # `connect` events. Missing this is silent and expensive: the whole app
            # runs on THIS engine, so without it `foreign_keys` stays off (so
            # ON DELETE CASCADE never fires and deleting a run orphans its spans)
            # and `synchronous` stays FULL (an fsync on every ingest commit).
            _apply_pragmas(_async_engine.sync_engine)
        _async_factory = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _async_engine


def async_session_factory() -> async_sessionmaker[AsyncSession]:
    async_engine()
    assert _async_factory is not None
    return _async_factory


def sync_engine() -> Engine:
    global _sync_engine, _sync_factory
    if _sync_engine is None:
        _sync_engine = create_engine(sync_db_url(), echo=False, future=True)
        if _sync_engine.dialect.name == "sqlite":
            _apply_pragmas(_sync_engine)
        _sync_factory = sessionmaker(_sync_engine, expire_on_commit=False)
    return _sync_engine


@asynccontextmanager
async def async_session() -> AsyncIterator[AsyncSession]:
    factory = async_session_factory()
    async with factory() as s:
        yield s


@contextmanager
def sync_session() -> Iterator[Session]:
    sync_engine()
    assert _sync_factory is not None
    with _sync_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Recording format versioning
#
# Every trace DB is stamped with the recording format version in SQLite's
# `PRAGMA user_version`. On open we refuse to silently read a DB written by a
# newer/incompatible Husk (loud failure) and migrate forward an older one. The
# schema is still created with create_all; these stepwise migrations cover shape
# changes that create_all cannot express (column renames, data backfills).
#
# Register a v(N) -> v(N+1) migration here when bumping RECORDING_FORMAT_VERSION.
# ---------------------------------------------------------------------------

_MIGRATIONS: dict[int, Callable[[Connection], None]] = {}


def _migrate_v1_to_v2(conn: Connection) -> None:
    """v1 -> v2: add the nullable, indexed ``runs.project_id`` column.

    Additive ALTER for existing SQLite trace DBs — ``create_all`` only creates
    missing tables, never adds columns to an existing one. Idempotent: skip the
    ALTER if the column is already present, so a partially-applied or legacy
    DB never crashes on boot."""
    cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(runs)")}
    if "project_id" not in cols:
        conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN project_id VARCHAR(40)")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_runs_project_id ON runs (project_id)"
    )


_MIGRATIONS[1] = _migrate_v1_to_v2


def _migration_steps(
    found: int,
    current: int = RECORDING_FORMAT_VERSION,
    available: dict[int, Callable[[Connection], None]] | None = None,
) -> list[int]:
    """Ordered `from`-versions to apply to bring `found` up to `current`.

    Returns [] when already current/newer. Raises RecordingFormatError if a step
    in the chain has no registered migration (so an upgrade can't silently skip).
    """
    avail = _MIGRATIONS if available is None else available
    if found >= current:
        return []
    steps: list[int] = []
    v = found
    while v < current:
        if v not in avail:
            raise RecordingFormatError(
                f"trace DB is recording format v{found}; no migration step from "
                f"v{v} to v{v + 1} (cannot upgrade to v{current})."
            )
        steps.append(v)
        v += 1
    return steps


def _ensure_recording_version(conn: Connection) -> None:
    """Validate, migrate if needed, and (re)stamp the DB's recording version."""
    found = int(conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
    if found > RECORDING_FORMAT_VERSION:
        # check_format_version raises a clear RecordingFormatError here.
        check_format_version(found, source="trace DB")
    elif 0 < found < RECORDING_FORMAT_VERSION:
        for v in _migration_steps(found):
            _MIGRATIONS[v](conn)
    # found == 0 (fresh or legacy-unstamped) or == current: just (re)stamp.
    conn.exec_driver_sql(f"PRAGMA user_version = {RECORDING_FORMAT_VERSION}")


def verify_recording_version(db_path: Path) -> int:
    """Read-only guard: confirm a trace DB on disk is readable by this build.

    Returns the effective recording version, or raises RecordingFormatError if
    the DB was written by a newer Husk. An unstamped DB (user_version 0) is
    treated as the current version (legacy DBs predate stamping but share the
    v1 shape). Used by `husk-studio doctor` and tests.
    """
    con = sqlite3.connect(str(db_path))
    try:
        found = int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        con.close()
    if found == 0:
        return RECORDING_FORMAT_VERSION
    return check_format_version(found, source=f"trace DB {Path(db_path).name}")


async def init_db() -> None:
    """Create tables and stamp/verify the recording format version."""
    import os

    from husk_studio_backend.config import husk_home
    from husk_studio_backend.db.models import Base

    engine = async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Recording-format versioning rides on SQLite's PRAGMA user_version. On
        # Postgres, create_all already builds the current schema, so skip it.
        if engine.dialect.name == "sqlite":
            await conn.run_sync(_ensure_recording_version)

    # The owner-only chmod + the on-disk DB file only exist for local SQLite.
    if engine.dialect.name != "sqlite":
        return

    # The trace DB holds cleartext prompts/completions/tool I/O. Lock it to the
    # owner (best-effort; on Windows the ~/.husk ACL is the real protection).
    try:
        os.chmod(husk_home() / "traces.db", 0o600)
    except OSError:
        pass
