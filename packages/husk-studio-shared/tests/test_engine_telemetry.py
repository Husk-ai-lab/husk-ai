"""The optional engine telemetry hook fires per node and is a no-op by default."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from husk_studio_shared.engine import LinearExecutor, LinearGraph, SnapshotStore


def _append(node: str):  # type: ignore[no-untyped-def]
    def _node(state: dict[str, Any]) -> dict[str, Any]:
        return {"trail": [*state.get("trail", []), node], node: True}

    return _node


def _build() -> LinearGraph:
    g = LinearGraph()
    g.add_node("a", _append("a"))
    g.add_node("b", _append("b"))
    g.add_node("c", _append("c"))
    return g


def test_on_node_fires_per_node_with_before_after_delta(tmp_path: Any) -> None:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))
    events: list[dict[str, Any]] = []

    @contextmanager
    def on_node(node: str, seq: int, before: dict[str, Any]):  # type: ignore[no-untyped-def]
        rec: dict[str, Any] = {"node": node, "seq": seq, "before": dict(before)}

        def record(after: dict[str, Any], delta: dict[str, Any], error: Any) -> None:
            rec["after"] = dict(after)
            rec["delta"] = dict(delta)
            rec["error"] = error

        yield record
        events.append(rec)

    ex = LinearExecutor(_build(), store, on_node=on_node)
    ex.run_full("t", {"topic": "x"})

    assert [e["node"] for e in events] == ["a", "b", "c"]
    assert [e["seq"] for e in events] == [0, 1, 2]
    # First node: before has only topic; after/delta carry its contribution.
    assert events[0]["before"] == {"topic": "x"}
    assert events[0]["delta"]["a"] is True
    assert events[0]["after"]["trail"] == ["a"]
    assert all(e["error"] is None for e in events)


def test_on_node_records_exception_and_reraises(tmp_path: Any) -> None:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))

    def boom(_state: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("nope")

    g = LinearGraph()
    g.add_node("a", _append("a"))
    g.add_node("boom", boom)

    captured: dict[str, Any] = {}

    @contextmanager
    def on_node(node: str, seq: int, before: dict[str, Any]):  # type: ignore[no-untyped-def]
        def record(after: dict[str, Any], delta: dict[str, Any], error: Any) -> None:
            captured[node] = error

        yield record

    ex = LinearExecutor(g, store, on_node=on_node)
    with pytest.raises(ValueError):
        ex.run_full("t", {})

    assert "a" in captured and captured["a"] is None
    assert isinstance(captured.get("boom"), ValueError)


def test_default_no_hook_is_unchanged(tmp_path: Any) -> None:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))
    ex = LinearExecutor(_build(), store)  # no on_node
    out = ex.run_full("t", {"topic": "x"})
    assert out["trail"] == ["a", "b", "c"]
