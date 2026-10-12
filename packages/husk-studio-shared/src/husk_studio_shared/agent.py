"""Zero-boilerplate Husk agent: decorate plain functions as graph nodes and get
time-travel replay (state snapshot per node, resume from any node) for free.

    from husk_studio_shared.agent import HuskAgent

    agent = HuskAgent("research")   # assign to a module global so replay can find it

    @agent.node
    def plan(state: dict) -> dict: ...

    @agent.node
    def answer(state: dict) -> dict: ...

    agent.invoke({"topic": "Rome"})

This wraps :mod:`husk_studio_shared.engine` (``LinearExecutor`` + ``SnapshotStore``) and
emits the OTel spans + ``husk.*`` attributes the Studio needs, so the backend can
resume the agent from any checkpoint with edited state — re-running only the
fork node and its successors. No manual span code, no rewrite onto the raw engine.

The same surface the older hand-written examples expose (``invoke`` /
``replay_from`` + a ``husk.graph_module`` root-span attribute) is generated here,
so ``husk_studio_backend.replay.graph_replay`` drives a decorated agent unchanged.
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentelemetry import trace

from husk_studio_shared.engine import LinearExecutor, LinearGraph, SnapshotStore
from husk_studio_shared.state_diff import diff_states
from husk_studio_shared.tracing import instrument

Node = Callable[[dict[str, Any]], dict[str, Any]]

_STATE_ATTR_CAP = 8000


def _safe_state_json(obj: object) -> str:
    s = json.dumps(obj, sort_keys=True, default=str)
    if len(s) > _STATE_ATTR_CAP:
        s = s[:_STATE_ATTR_CAP] + f"…[+{len(s) - _STATE_ATTR_CAP} chars]"
    return s


def _snapshot_db_path(name: str) -> str:
    home = Path(os.environ.get("HUSK_HOME", str(Path.home() / ".husk")))
    home.mkdir(parents=True, exist_ok=True)
    return str(home / f"{name}.sqlite")


class HuskAgent:
    """A linear agent built from ``@agent.node``-decorated functions."""

    # Marker so the replay dispatcher uses the Husk calling convention
    # (invoke(state, thread_id=) / replay_from(...)) rather than LangGraph's.
    _husk_agent = True

    def __init__(
        self, name: str, *, service_name: str | None = None, snapshot_db: str | None = None
    ) -> None:
        self.name = name
        self._service = service_name or name
        self._graph = LinearGraph()
        self._snapshot_db = snapshot_db
        self._file: str | None = None
        self._globals: dict[str, Any] | None = None
        self._tracer: Any = None
        self._store: SnapshotStore | None = None
        self._store_lock = threading.Lock()

    # ── registration ────────────────────────────────────────────────────────
    def node(self, fn: Node) -> Node:
        """Register ``fn`` as the next node in the chain (named after the function)."""
        self._graph.add_node(fn.__name__, fn)
        if self._file is None:
            # __globals__ is the defining module's namespace — used to find the
            # name this agent is bound to (for husk.graph_module). Captured from
            # the function, so it survives the backend re-importing the file.
            self._file = str(Path(inspect.getfile(fn)).resolve())
            self._globals = fn.__globals__
        return fn

    # ── internals ─────────────────────────────────────────────────────────--
    def _get_tracer(self) -> Any:
        if self._tracer is None:
            self._tracer = instrument(service_name=self._service)
        return self._tracer

    def _get_store(self) -> SnapshotStore:
        with self._store_lock:
            if self._store is None:
                self._store = SnapshotStore(self._snapshot_db or _snapshot_db_path(self.name))
            return self._store

    def _graph_module(self) -> str:
        symbol = self.name
        if self._globals:
            symbol = next((k for k, v in self._globals.items() if v is self), self.name)
        return f"{self._file}:{symbol}"

    def _node_telemetry(self, tracer: Any) -> Callable[..., Any]:
        @contextmanager
        def on_node(node: str, seq: int, before: dict[str, Any]) -> Iterator[Any]:
            with tracer.start_as_current_span(f"node:{node}") as span:
                span.set_attribute("husk.span_kind", "graph_node")
                span.set_attribute("husk.node", node)
                span.set_attribute("husk.node_seq", seq)
                span.set_attribute("husk.state_before", _safe_state_json(before))

                def record(after: dict[str, Any], delta: dict[str, Any], error: BaseException | None) -> None:
                    span.set_attribute("husk.state_after", _safe_state_json(after))
                    span.set_attribute("husk.state_diff", _safe_state_json(diff_states(before, after)))
                    if error is not None:
                        span.set_attribute(
                            "husk.error.traceback",
                            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
                        )
                        span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))

                yield record

        return on_node

    def _set_topology(self, root: Any) -> None:
        names = self._graph.node_names
        root.set_attribute("husk.graph.nodes", json.dumps(names))
        root.set_attribute(
            "husk.graph.edges",
            json.dumps([[a, b] for a, b in zip(names, names[1:], strict=False)]),
        )

    def _flush(self) -> None:
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()

    # ── public api ──────────────────────────────────────────────────────────
    def invoke(self, state: dict[str, Any], thread_id: str | None = None) -> dict[str, Any]:
        """Run the whole chain from scratch, snapshotting after each node."""
        tracer = self._get_tracer()
        tid = thread_id or str(uuid.uuid4())
        executor = LinearExecutor(self._graph, self._get_store(), on_node=self._node_telemetry(tracer))
        with tracer.start_as_current_span("agent.run") as root:
            root.set_attribute("service.name", self._service)
            root.set_attribute("husk.thread_id", tid)
            root.set_attribute("husk.graph_module", self._graph_module())
            root.set_attribute("husk.replay.engine", "husk-native")
            self._set_topology(root)
            result = executor.run_full(tid, state)
            root.set_attribute("husk.final_state", str(result))
        self._flush()
        return {"thread_id": tid, "state": dict(result)}

    def replay_from(
        self, *, state_override: dict[str, Any], parent_thread_id: str, fork_node: str
    ) -> dict[str, Any]:
        """Resume ``parent_thread_id`` from its snapshot and re-run ``fork_node`` onward.

        Upstream nodes are not re-run: their state comes from the snapshot, so they
        emit no spans and consume no tokens. This is the token-bypass primitive.
        """
        tracer = self._get_tracer()
        executor = LinearExecutor(self._graph, self._get_store(), on_node=self._node_telemetry(tracer))
        child_id = str(uuid.uuid4())
        child_thread_id = f"{parent_thread_id}::replay::{child_id}"
        with tracer.start_as_current_span("agent.run") as root:
            root.set_attribute("service.name", self._service)
            root.set_attribute("husk.thread_id", parent_thread_id)
            root.set_attribute("husk.graph_module", self._graph_module())
            root.set_attribute("husk.replay.engine", "husk-native")
            root.set_attribute("husk.replay.child_id", child_id)
            root.set_attribute("husk.replay.fork_node", fork_node)
            self._set_topology(root)
            result = executor.resume(
                parent_thread_id=parent_thread_id,
                child_thread_id=child_thread_id,
                fork_node=fork_node,
                patch=dict(state_override),
            )
            root.set_attribute("husk.final_state", str(result))
        self._flush()
        return {"thread_id": parent_thread_id, "child_id": child_id, "state": dict(result)}
