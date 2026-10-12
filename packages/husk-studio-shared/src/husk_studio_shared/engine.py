"""Husk's own checkpoint/replay engine (linear MVP).

This module is the substrate behind Husk's modify-and-replay primitive: a small,
framework-agnostic executor plus a local SQLite snapshot store that Husk owns end
to end. Nothing here depends on any agent framework, OpenTelemetry, or the
network: nodes are plain callables and snapshots live in a local file.

Scope (MVP): linear node chains, which is all the benchmark graph needs. A node
is a callable ``(state) -> delta`` returning a partial-state dict. The executor
runs nodes in order, merges each delta into a running state dict (last write
wins), and writes a snapshot after every node. ``resume`` reloads the snapshot
taken after a fork node's predecessor, applies a patch, and re-executes exactly
the fork node and its successors -- the upstream nodes are never called, so (when
each node emits its own telemetry) they produce no spans and consume no tokens.
That bypass is what the token metric measures; the engine earns it by simply not
calling the skipped nodes.

Deliberately deferred: general-DAG topologies (a topological order over explicit
edges) and a compatibility adapter for third-party graph frameworks.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Final

# A node is a step: given the running state, return a partial-state delta. The
# executor merges that delta into the running state (last write wins).
Node = Callable[[dict[str, Any]], dict[str, Any]]

# Optional per-node telemetry callback the recorder (e.g. the OTel-instrumented
# graph wiring) injects. The executor stays framework-agnostic: it merely opens
# the context manager around each node's execution and, if the manager yields a
# callable, invokes it with (after_state, delta, error) once the node finishes.
#
#   on_node(node_name, seq, before_state) -> context manager yielding a
#       record(after_state: dict, delta: dict, error: BaseException | None) -> None
#
# Making the manager *current* for the duration of the node call is what lets the
# node's own spans nest under a per-node "graph_node" span. ``None`` (the default)
# is a true no-op, so every existing caller behaves byte-identically.
RecordFn = Callable[[dict[str, Any], dict[str, Any], "BaseException | None"], None]
NodeTelemetry = Callable[[str, int, dict[str, Any]], AbstractContextManager[Any]]


class _StartSentinel:
    """Marker for the graph entry point: the predecessor of the first node."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return "START"


#: The predecessor of the first node. Resuming "at START" means a full re-run.
START: Final[_StartSentinel] = _StartSentinel()

#: Reserved node key under which ``run_full`` stores the run's initial state, so a
#: START resume (full re-run) can inherit the parent's original inputs (the patch
#: is merged on top of this stored initial state).
_INITIAL_NODE: Final[str] = "__start__"


class SnapshotNotFound(LookupError):
    """Raised when a resume references a (thread_id, node) with no stored snapshot."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class LinearGraph:
    """An ordered chain of named nodes. ``add_node`` order IS execution order.

    Linear-only by design for the MVP; a general DAG (topological order over
    explicit edges) is a deliberate later step. The benchmark graph is linear,
    so a chain is sufficient and keeps the off-by-one trivial to reason about.
    """

    def __init__(self) -> None:
        self._nodes: list[tuple[str, Node]] = []
        self._index: dict[str, int] = {}

    def add_node(self, name: str, fn: Node) -> LinearGraph:
        if name in self._index:
            raise ValueError(f"duplicate node name: {name!r}")
        self._index[name] = len(self._nodes)
        self._nodes.append((name, fn))
        return self

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def node_names(self) -> list[str]:
        return [name for name, _ in self._nodes]

    def index(self, name: str) -> int:
        try:
            return self._index[name]
        except KeyError:
            raise KeyError(f"unknown node: {name!r}") from None

    def node_at(self, i: int) -> tuple[str, Node]:
        return self._nodes[i]

    def predecessor(self, name: str) -> str | _StartSentinel:
        """The node that runs immediately before ``name`` (START for the first)."""
        i = self.index(name)
        return START if i == 0 else self._nodes[i - 1][0]

    def successors(self, name: str) -> list[str]:
        """Every node that runs after ``name``, in order."""
        i = self.index(name)
        return [n for n, _ in self._nodes[i + 1 :]]


class SnapshotStore:
    """Local SQLite snapshot store, keyed by (thread_id, node).

    One row per (thread_id, node): the full merged state after that node ran,
    serialized as JSON. This is Husk's snapshot store for the primitive.
    Table shape:

        snapshots(thread_id, seq, node, state_json, created_at)

    A fresh connection is opened per operation so the store is safe to share
    across the benchmark's worker threads; ``busy_timeout`` + WAL handle
    concurrent writers. This is a distinct, engine-owned DB file from the
    studio backend's ``traces.db`` (whose unrelated ``snapshots`` table keys on
    ingested run_id/span_id, which do not exist at execution time).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=30.0)
        con.execute("PRAGMA busy_timeout = 30000")
        return con

    def _ensure(self) -> None:
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "  thread_id  TEXT    NOT NULL,"
                "  seq        INTEGER NOT NULL,"
                "  node       TEXT    NOT NULL,"
                "  state_json TEXT    NOT NULL,"
                "  created_at INTEGER NOT NULL,"
                "  PRIMARY KEY (thread_id, node)"
                ")"
            )
            con.commit()
        finally:
            con.close()

    def put(self, thread_id: str, seq: int, node: str, state: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(state), sort_keys=True, default=str)
        con = self._connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(thread_id, seq, node, state_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (thread_id, seq, node, payload, _now_ms()),
            )
            con.commit()
        finally:
            con.close()

    def get(self, thread_id: str, node: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT state_json FROM snapshots WHERE thread_id = ? AND node = ?",
                (thread_id, node),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        data: dict[str, Any] = json.loads(row[0])
        return data


class LinearExecutor:
    """Runs a :class:`LinearGraph`, snapshotting after every node, and resumes.

    The executor is telemetry-agnostic: it only calls the node callables. Any
    spans or tokens a node emits are the node's own doing, so skipping a node
    (never calling it) is precisely what produces zero spans and zero tokens
    upstream. That is the whole basis of the token-bypass measurement.
    """

    def __init__(
        self,
        graph: LinearGraph,
        store: SnapshotStore,
        *,
        on_node: NodeTelemetry | None = None,
    ) -> None:
        self._graph = graph
        self._store = store
        self._on_node = on_node

    def run_full(self, thread_id: str, initial_state: Mapping[str, Any]) -> dict[str, Any]:
        """Run the whole chain from the first node, snapshotting after each.

        Also records the initial state under ``_INITIAL_NODE`` so a later START
        resume (full re-run) can inherit these inputs.
        """
        state = dict(initial_state)
        self._store.put(thread_id, -1, _INITIAL_NODE, state)
        return self._run_from(thread_id, 0, state)

    def resume(
        self,
        *,
        parent_thread_id: str,
        child_thread_id: str,
        fork_node: str,
        patch: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Re-run exactly ``fork_node`` and its successors -- nothing upstream.

        Loads the snapshot taken after ``fork_node``'s predecessor (the state that
        was about to enter ``fork_node`` in the parent run), applies ``patch``, then
        executes from ``fork_node`` onward under ``child_thread_id`` (so the parent's
        snapshots stay intact and can be re-forked). When the predecessor is START
        (``fork_node`` is the first node) there is no upstream snapshot and the
        patch alone seeds a full re-run -- the deliberate negative control.

        The off-by-one is deliberate: to re-run node X we resume at X's
        predecessor's snapshot (the state X consumed in the parent run).
        """
        predecessor = self._graph.predecessor(fork_node)
        if isinstance(predecessor, _StartSentinel):
            # Full re-run: seed from the parent's recorded initial state (if any)
            # and apply the patch on top.
            initial = self._store.get(parent_thread_id, _INITIAL_NODE) or {}
            state: dict[str, Any] = {**initial, **patch}
        else:
            base = self._store.get(parent_thread_id, predecessor)
            if base is None:
                raise SnapshotNotFound(
                    f"no snapshot for thread {parent_thread_id!r} at node {predecessor!r} "
                    f"(predecessor of {fork_node!r})"
                )
            state = {**base, **patch}
        return self._run_from(child_thread_id, self._graph.index(fork_node), state)

    def _run_from(
        self, thread_id: str, start_index: int, state: dict[str, Any]
    ) -> dict[str, Any]:
        for i in range(start_index, len(self._graph)):
            name, fn = self._graph.node_at(i)
            before = dict(state)
            cm: AbstractContextManager[Any] = (
                self._on_node(name, i, before)
                if self._on_node is not None
                else nullcontext()
            )
            with cm as record:
                try:
                    delta = fn(state)
                except BaseException as exc:
                    # after == before: the failed node committed no delta.
                    if callable(record):
                        record(before, {}, exc)
                    raise
                state.update(delta)
                self._store.put(thread_id, i, name, state)
                if callable(record):
                    record(dict(state), delta, None)
        return state
