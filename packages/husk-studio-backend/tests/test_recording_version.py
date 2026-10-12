"""Recording-format versioning: stamp, verify, migrate-or-fail-loudly.

Backs WS2: a trace DB written by a newer/incompatible Husk must be refused with
a clear error rather than silently misread, and an older one must follow a
complete migration chain (no silent gaps).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from husk_studio_backend.db.engine import (
    _ensure_recording_version,
    _migration_steps,
    verify_recording_version,
)
from husk_studio_backend.db.models import Base
from husk_studio_shared import (
    RECORDING_FORMAT_VERSION,
    RecordingFormatError,
    check_format_version,
)


def _user_version(db: Path) -> int:
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
    finally:
        con.close()


# ── version policy (pure) ────────────────────────────────────────────────────

def test_check_format_version_equal_passes() -> None:
    assert check_format_version(RECORDING_FORMAT_VERSION) == RECORDING_FORMAT_VERSION


def test_check_format_version_newer_fails_loudly() -> None:
    with pytest.raises(RecordingFormatError, match="understands only up to"):
        check_format_version(RECORDING_FORMAT_VERSION + 1, source="trace DB")


def test_check_format_version_older_allows_migration() -> None:
    # 0 stands in as "older than current": accepted when migration is allowed,
    # refused when it is disabled.
    assert check_format_version(0, allow_migration=True) == 0
    with pytest.raises(RecordingFormatError, match="migration was disabled"):
        check_format_version(0, allow_migration=False)


def test_migration_steps_complete_chain() -> None:
    avail = {1: lambda c: None, 2: lambda c: None}
    assert _migration_steps(1, current=3, available=avail) == [1, 2]


def test_migration_steps_missing_step_fails_loudly() -> None:
    avail = {1: lambda c: None}  # no 2 -> 3 step
    with pytest.raises(RecordingFormatError, match="no migration step"):
        _migration_steps(1, current=3, available=avail)


def test_migration_steps_current_is_noop() -> None:
    assert _migration_steps(RECORDING_FORMAT_VERSION) == []


# ── on-disk stamp + verify (integration) ─────────────────────────────────────

def _fresh_db(tmp_path: Path) -> Path:
    db = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        Base.metadata.create_all(conn)
        _ensure_recording_version(conn)
    eng.dispose()
    return db


def test_init_stamps_current_version(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    assert _user_version(db) == RECORDING_FORMAT_VERSION
    assert verify_recording_version(db) == RECORDING_FORMAT_VERSION


def test_unstamped_legacy_db_reads_as_current(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    create_engine(f"sqlite:///{db}").dispose()  # user_version stays 0
    assert _user_version(db) == 0
    assert verify_recording_version(db) == RECORDING_FORMAT_VERSION


def test_too_new_db_is_refused(tmp_path: Path) -> None:
    db = _fresh_db(tmp_path)
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {RECORDING_FORMAT_VERSION + 5}")
    eng.dispose()
    with pytest.raises(RecordingFormatError):
        verify_recording_version(db)


def _runs_columns(db: Path) -> set[str]:
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        return {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    finally:
        con.close()


def test_v1_db_migrates_to_v2_adding_project_id(tmp_path: Path) -> None:
    # The v1 -> v2 migration is an additive ALTER (create_all never adds columns
    # to an existing table), so a pre-project_id DB must gain the column on open.
    assert RECORDING_FORMAT_VERSION >= 2  # this test exercises the v1 -> v2 step
    db = tmp_path / "v1.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        # A v1-shaped runs table: no project_id, stamped recording v1.
        conn.exec_driver_sql(
            'CREATE TABLE runs (id VARCHAR PRIMARY KEY, started_at BIGINT, '
            'status VARCHAR, "metadata" JSON)'
        )
        conn.exec_driver_sql(
            "INSERT INTO runs (id, started_at, status) VALUES ('old', 1, 'success')"
        )
        conn.exec_driver_sql("PRAGMA user_version = 1")
    with eng.begin() as conn:
        _ensure_recording_version(conn)  # 0 < 1 < current -> run the migration chain
    eng.dispose()

    assert "project_id" in _runs_columns(db)  # column added
    assert _user_version(db) == RECORDING_FORMAT_VERSION  # restamped to current

    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        # Pre-existing rows backfill to NULL = "All projects".
        assert con.execute("SELECT project_id FROM runs WHERE id='old'").fetchone()[0] is None
    finally:
        con.close()


def test_v1_to_v2_migration_is_idempotent(tmp_path: Path) -> None:
    # Re-running the guard on an already-current DB must not fail (the ALTER is
    # guarded by a column-existence check).
    db = _fresh_db(tmp_path)
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        _ensure_recording_version(conn)
        _ensure_recording_version(conn)
    eng.dispose()
    assert "project_id" in _runs_columns(db)
