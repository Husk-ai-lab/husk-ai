from __future__ import annotations

import os
from pathlib import Path


def husk_home() -> Path:
    """Return the Husk home directory (~/.husk), creating it if missing.

    Override with env var HUSK_HOME for tests or custom layouts.
    """
    custom = os.environ.get("HUSK_HOME")
    base = Path(custom) if custom else Path.home() / ".husk"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return husk_home() / "traces.db"


def runs_dir() -> Path:
    d = husk_home() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_dir(run_id: str) -> Path:
    d = runs_dir() / run_id
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    (d / "snapshots").mkdir(parents=True, exist_ok=True)
    (d / "cassettes").mkdir(parents=True, exist_ok=True)
    return d
