"""Export a recorded benchmark run to a committed, offline fixture.

The published hero numbers (token bypass, wall-time speed-up, replay success) are
computed by ``hero_report.py`` over a live ``~/.husk/traces.db``. That DB is not
committed, so the numbers could not be regenerated without a live API key. This
script freezes the canonical run into a small, reviewable, version-stamped
fixture under ``benchmark/fixtures/`` so ``benchmark/reproduce.py`` can rebuild a
temp DB and recompute the exact same numbers offline, with no provider key.

Only the columns the metrics actually read are exported. The bulky per-span
prompt/completion text (``input_inline`` / ``output_inline`` / ``*_ref``) is
dropped — it is irrelevant to D1–D5 and would bloat the fixture ~10x.

Usage:
    uv run python benchmark/export_fixture.py [--db PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from husk_studio_shared import RECORDING_FORMAT_VERSION  # noqa: E402

DEFAULT_DB = Path.home() / ".husk" / "traces.db"
DEFAULT_OUT = Path(__file__).resolve().parent / "fixtures" / "canonical_run"

# Bulky span columns the metrics never read — dropped to keep the fixture small.
_SPAN_DROP = {"input_ref", "input_inline", "output_ref", "output_inline"}
_TABLES = ("runs", "spans", "branches")


def _export_table(con: sqlite3.Connection, table: str, drop: set[str]) -> tuple[list[str], list[dict]]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c not in drop]
    order = "started_at, id" if table in ("runs", "spans") else "created_at, id"
    rows = con.execute(f"SELECT {', '.join(keep)} FROM {table} ORDER BY {order}").fetchall()
    return keep, [dict(zip(keep, r, strict=True)) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description="Export a recorded run to an offline fixture.")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args()

    if not args.db.exists():
        print(f"ERROR: source DB {args.db} not found.", file=sys.stderr)
        return 2

    con = sqlite3.connect(str(args.db))
    args.out.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for table in _TABLES:
        drop = _SPAN_DROP if table == "spans" else set()
        _, rows = _export_table(con, table, drop)
        counts[table] = len(rows)
        with (args.out / f"{table}.jsonl").open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    meta = {
        "format_version": RECORDING_FORMAT_VERSION,
        "source_db": args.db.name,
        "original_db_size_bytes": args.db.stat().st_size,
        "row_counts": counts,
        "note": (
            "Canonical published benchmark run (OpenRouter Llama-3.3-70B + 3.1-8B). "
            "Per-span prompt/completion text stripped; metrics use only "
            "token/timing/attrs columns. Regenerate with export_fixture.py."
        ),
    }
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    con.close()

    print(f"Wrote fixture to {args.out}")
    for t, n in counts.items():
        print(f"  {t}: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
