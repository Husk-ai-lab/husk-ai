"""Offline benchmark fixtures.

`canonical_run/` is a version-stamped, committed snapshot of the published
benchmark run (runs/spans/branches), exported by `benchmark/export_fixture.py`.
`load_fixture` rebuilds a throwaway SQLite DB from it using the real backend
schema, so `benchmark/reproduce.py` and the test suite can recompute the exact
hero numbers offline — no provider key, no network.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine

from husk_studio_backend.db.models import Base
from husk_studio_shared import RECORDING_FORMAT_VERSION, check_format_version

FIXTURE_ROOT = Path(__file__).resolve().parent
CANONICAL_RUN = FIXTURE_ROOT / "canonical_run"

# Insert order respects FK direction (runs before spans/branches).
_TABLES = ("runs", "spans", "branches")


def load_fixture(dest_db: Path, fixture_dir: Path = CANONICAL_RUN) -> Path:
    """Build a fresh SQLite DB at `dest_db` from the JSONL fixture in `fixture_dir`.

    Validates the fixture's recording format version (loud failure on mismatch),
    creates the canonical schema from the backend models, stamps the DB's
    `user_version`, and bulk-loads the rows. Returns `dest_db`.
    """
    meta = json.loads((fixture_dir / "meta.json").read_text(encoding="utf-8"))
    check_format_version(int(meta.get("format_version", 0)), source=f"fixture {fixture_dir.name}")

    dest_db = Path(dest_db)
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if dest_db.exists():
        dest_db.unlink()

    engine = create_engine(f"sqlite:///{dest_db}")
    Base.metadata.create_all(engine)
    engine.dispose()

    con = sqlite3.connect(str(dest_db))
    try:
        con.execute(f"PRAGMA user_version = {RECORDING_FORMAT_VERSION}")
        for table in _TABLES:
            path = fixture_dir / f"{table}.jsonl"
            if not path.exists():
                continue
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if not rows:
                continue
            cols = list(rows[0].keys())
            placeholders = ", ".join(["?"] * len(cols))
            con.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                [[r.get(c) for c in cols] for r in rows],
            )
        con.commit()
    finally:
        con.close()
    return dest_db
