"""Unit tests for Husk's own checkpoint/replay engine.

These exercise the engine in isolation (no OTel, no benchmark, no network) so the
load-bearing property -- "resuming at a fork re-runs exactly the fork node and its
successors, never anything upstream" -- is proven for Husk's own engine directly.
The benchmark-level structural test in ``benchmark/tests/test_determinism.py``
then proves the same end to end.
"""

from __future__ import annotations

from typing import Any

import pytest

from husk_studio_shared.engine import (
    START,
    LinearExecutor,
    LinearGraph,
    SnapshotNotFound,
    SnapshotStore,
)


def _append(node: str):  # type: ignore[no-untyped-def]
    """A node that records its own execution in the ``trail`` channel."""

    def _node(state: dict[str, Any]) -> dict[str, Any]:
        return {"trail": [*state.get("trail", []), node], node: True}

    return _node


def _build() -> LinearGraph:
    g = LinearGraph()
    g.add_node("a", _append("a"))
    g.add_node("b", _append("b"))
    g.add_node("c", _append("c"))
    g.add_node("d", _append("d"))
    return g


def _executor(tmp_path: Any) -> LinearExecutor:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))
    return LinearExecutor(_build(), store)


def test_graph_topology() -> None:
    g = _build()
    assert g.node_names == ["a", "b", "c", "d"]
    assert g.predecessor("a") is START
    assert g.predecessor("c") == "b"
    assert g.successors("b") == ["c", "d"]
    assert g.successors("d") == []


def test_run_full_executes_every_node_in_order(tmp_path: Any) -> None:
    ex = _executor(tmp_path)
    final = ex.run_full("t1", {"topic": "x"})
    assert final["trail"] == ["a", "b", "c", "d"]
    assert final["topic"] == "x"


def test_resume_reruns_exactly_fork_and_successors(tmp_path: Any) -> None:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))
    ex = LinearExecutor(_build(), store)
    ex.run_full("parent", {"topic": "x"})

    # Resume at "c": its predecessor "b" snapshot seeds the state; only c, d run.
    out = ex.resume(
        parent_thread_id="parent",
        child_thread_id="child",
        fork_node="c",
        patch={"patched": True},
    )
    assert out["trail"] == ["a", "b", "c", "d"]  # a,b from snapshot; c,d freshly run
    assert out["patched"] is True
    # The child only ever appended c and d -- a and b were NOT re-run.
    child_c = store.get("child", "c")
    assert child_c is not None
    assert child_c["trail"] == ["a", "b", "c"]
    # The child's earliest snapshot is the fork node, never an upstream one.
    assert store.get("child", "a") is None
    assert store.get("child", "b") is None


def test_resume_at_first_node_is_full_rerun(tmp_path: Any) -> None:
    ex = _executor(tmp_path)
    ex.run_full("parent", {"topic": "x"})
    # Predecessor of "a" is START -> patch alone seeds a full re-run.
    out = ex.resume(
        parent_thread_id="parent",
        child_thread_id="child",
        fork_node="a",
        patch={"topic": "y"},
    )
    assert out["trail"] == ["a", "b", "c", "d"]
    assert out["topic"] == "y"


def test_resume_without_snapshot_raises(tmp_path: Any) -> None:
    ex = _executor(tmp_path)
    with pytest.raises(SnapshotNotFound):
        ex.resume(
            parent_thread_id="never-ran",
            child_thread_id="child",
            fork_node="c",
            patch={},
        )


def test_snapshot_store_roundtrip(tmp_path: Any) -> None:
    store = SnapshotStore(str(tmp_path / "snap.sqlite"))
    store.put("t", 0, "a", {"x": 1, "y": ["z"]})
    assert store.get("t", "a") == {"x": 1, "y": ["z"]}
    assert store.get("t", "missing") is None
    # INSERT OR REPLACE: re-running the same thread/node overwrites in place.
    store.put("t", 0, "a", {"x": 2})
    assert store.get("t", "a") == {"x": 2}
