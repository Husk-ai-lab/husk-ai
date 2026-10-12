"""Structural diff between two agent-state dicts.

Used by the engine telemetry hook (to record what a node changed) and by the
graph API / debugger context (to show and reason about per-node state deltas).
Values are coerced to JSON-safe form with ``default=str`` so the diff matches the
serialization the snapshot store already uses (engine.py ``json.dumps(..., default=str)``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypedDict


class StateDiff(TypedDict):
    added: dict[str, Any]  # key -> new value (absent in before)
    removed: dict[str, Any]  # key -> old value (absent in after)
    changed: dict[str, dict[str, Any]]  # key -> {"from": old, "to": new}


def _jsonable(value: Any) -> Any:
    """Round-trip through JSON with ``default=str`` so comparisons are stable."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def diff_states(before: Mapping[str, Any], after: Mapping[str, Any]) -> StateDiff:
    """Compute added/removed/changed keys between two state mappings.

    Comparison is by JSON-normalized value, so e.g. a tuple that became a list of
    the same items is not reported as a change.
    """
    before_keys = set(before.keys())
    after_keys = set(after.keys())

    added = {k: _jsonable(after[k]) for k in after_keys - before_keys}
    removed = {k: _jsonable(before[k]) for k in before_keys - after_keys}

    changed: dict[str, dict[str, Any]] = {}
    for k in before_keys & after_keys:
        b = _jsonable(before[k])
        a = _jsonable(after[k])
        if b != a:
            changed[k] = {"from": b, "to": a}

    return {"added": added, "removed": removed, "changed": changed}


def diff_is_empty(diff: StateDiff) -> bool:
    return not (diff["added"] or diff["removed"] or diff["changed"])
