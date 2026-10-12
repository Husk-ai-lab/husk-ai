"""@husk.node / HuskAgent: the decorators must reproduce the hand-wired example's
behavior — per-node spans with state attrs, a replayable root span, and a resume
that re-runs only the fork node + successors (the token-bypass primitive)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> InMemorySpanExporter:
    """Capture OTel spans in-memory and keep snapshots under a temp home."""
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # husk_studio_shared.instrument() reuses an already-set real provider, so set ours.
    import opentelemetry.trace as _t
    from opentelemetry.util._once import Once

    _t._TRACER_PROVIDER = None
    _t._TRACER_PROVIDER_SET_ONCE = Once()
    trace.set_tracer_provider(provider)
    yield exporter
    _t._TRACER_PROVIDER = None
    _t._TRACER_PROVIDER_SET_ONCE = Once()


from husk_studio_shared.agent import HuskAgent  # noqa: E402

# A module-global agent, as in a real graph file — the dispatcher resolves
# husk.graph_module to the global name it is bound to ("module_agent").
module_agent = HuskAgent("modname")


@module_agent.node
def step(state: dict) -> dict:
    return {"x": state.get("seed", 0) + 1}


def _build_agent():  # type: ignore[no-untyped-def]
    agent = HuskAgent("test-agent", snapshot_db=None)

    @agent.node
    def first(state: dict) -> dict:
        return {"a": state["seed"] + 1}

    @agent.node
    def second(state: dict) -> dict:
        return {"b": state["a"] * 10}

    return agent


def test_invoke_runs_chain_and_snapshots(spans: InMemorySpanExporter, tmp_path: Path) -> None:
    agent = _build_agent()
    result = agent.invoke({"seed": 1})
    assert result["state"] == {"seed": 1, "a": 2, "b": 20}
    assert result["thread_id"]

    names = {s.name for s in spans.get_finished_spans()}
    assert "agent.run" in names
    assert {"node:first", "node:second"} <= names

    root = next(s for s in spans.get_finished_spans() if s.name == "agent.run")
    attrs = dict(root.attributes or {})
    assert attrs["husk.replay.engine"] == "husk-native"
    assert attrs["husk.thread_id"] == result["thread_id"]
    # graph_module points at THIS file; a locally-scoped agent falls back to its
    # name (a module-global agent resolves to its variable name — see below).
    gm = attrs["husk.graph_module"]
    assert "test_agent.py" in gm and gm.endswith(":test-agent")
    assert json.loads(attrs["husk.graph.nodes"]) == ["first", "second"]

    node = next(s for s in spans.get_finished_spans() if s.name == "node:second")
    nattrs = dict(node.attributes or {})
    assert nattrs["husk.node"] == "second"
    assert nattrs["husk.span_kind"] == "graph_node"
    assert "husk.state_diff" in nattrs


def test_resume_reruns_only_fork_node_onward(spans: InMemorySpanExporter, tmp_path: Path) -> None:
    agent = _build_agent()
    parent = agent.invoke({"seed": 1})
    tid = parent["thread_id"]
    spans.clear()

    # Resume at `second` with a patched `a`: only `second` should re-run. `first`
    # is served from the parent snapshot — so no node:first span this time.
    out = agent.replay_from(state_override={"a": 5}, parent_thread_id=tid, fork_node="second")
    assert out["state"]["b"] == 50  # 5 * 10 — used the patched value
    assert out["child_id"]

    names = [s.name for s in spans.get_finished_spans()]
    assert "node:second" in names
    assert "node:first" not in names  # upstream node bypassed → zero spans/tokens


def test_graph_module_uses_module_global_name(spans: InMemorySpanExporter, tmp_path: Path) -> None:
    module_agent._store = None  # fresh snapshot store under this test's HUSK_HOME
    module_agent.invoke({"seed": 0})
    root = next(s for s in spans.get_finished_spans() if s.name == "agent.run")
    gm = dict(root.attributes or {})["husk.graph_module"]
    # Resolved to the global the agent is bound to, so the backend can re-import
    # the file and getattr(module, "module_agent") to drive a replay.
    assert gm.endswith(":module_agent")
