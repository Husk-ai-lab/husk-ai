"""Turn a run's spans into a structured per-node graph.

This is the single source of truth that both the graph visualization API
(``api/graph.py``) and the debugger context assembler (``debugger/context_assembler.py``)
build on, so the UI and the LLM see the same node model.

A node is reconstructed from the spans that carry ``husk.node``:
  * a ``graph_node`` wrapper span (emitted by the engine telemetry hook) carries
    ``husk.state_before`` / ``husk.state_after`` / ``husk.state_diff`` /
    ``husk.node_seq`` and wraps the node's execution;
  * the node's own ``llm`` / ``tool`` spans (its model and tool calls) nest under
    it and supply prompts, responses, token counts, and tool args/results.

Topology (nodes + edges, including conditional edges/routing when recoverable)
is read from the root ``agent.run`` span attrs (``husk.graph.nodes`` etc.). When
those are absent (older runs, or frameworks that don't emit them) it degrades to
the executed order. A topology node with no span in the run is ``skipped`` — which
is exactly what a downstream-fork replay produces upstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from husk_studio_backend.db.models import RunRow, SpanRow
from husk_studio_shared.pricing import cost_usd

ROOT_SPAN_NAME = "agent.run"


def _loads(value: Any) -> Any:
    """Attrs may store JSON as a string (OTel primitive). Parse defensively."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _is_wrapper(span: SpanRow) -> bool:
    attrs = span.attrs or {}
    return span.kind == "graph_node" or "husk.state_before" in attrs


@dataclass
class ModelCall:
    span_id: str
    model: str | None
    provider: str | None
    tokens_in: int | None
    tokens_out: int | None
    prompt: Any
    response: Any
    status: str
    error: dict[str, Any] | None


@dataclass
class ToolCall:
    span_id: str
    name: str
    args: Any
    result: Any
    status: str
    error: dict[str, Any] | None


@dataclass
class NodeView:
    id: str
    name: str
    seq: int
    status: str  # success | error | skipped | running
    span_id: str | None
    started_at: int | None
    finished_at: int | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    is_failure_point: bool
    state_before: Any
    state_after: Any
    state_diff: Any
    model_calls: list[ModelCall] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: dict[str, Any] | None = None
    traceback: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return self.finished_at - self.started_at


@dataclass
class RunGraph:
    run_id: str
    nodes: list[NodeView]
    edges: list[dict[str, Any]]
    executed_path: list[str]
    failure: dict[str, Any] | None
    recursion_limit_hit: bool
    run_status: str
    run_error: str | None


def _model_call(span: SpanRow) -> ModelCall:
    inp = span.input_inline if isinstance(span.input_inline, dict) else {}
    out = span.output_inline if isinstance(span.output_inline, dict) else {}
    return ModelCall(
        span_id=span.id,
        model=span.model,
        provider=span.provider,
        tokens_in=span.tokens_in,
        tokens_out=span.tokens_out,
        prompt=inp.get("messages", span.input_inline),
        response=out.get("choices", span.output_inline),
        status=span.status,
        error=span.error_payload,
    )


def _tool_call(span: SpanRow) -> ToolCall:
    inp = span.input_inline if isinstance(span.input_inline, dict) else {}
    msgs = inp.get("messages") if isinstance(inp, dict) else None
    tool_msg = None
    if isinstance(msgs, list):
        tool_msg = next((m for m in msgs if isinstance(m, dict) and m.get("role") == "tool"), None)
    return ToolCall(
        span_id=span.id,
        name=(span.attrs or {}).get("gen_ai.tool.name") or span.name,
        args=(tool_msg or {}).get("arguments"),
        result=(tool_msg or {}).get("content") if tool_msg else span.output_inline,
        status=span.status,
        error=span.error_payload,
    )


def _root_attrs(spans: list[SpanRow]) -> dict[str, Any]:
    for s in spans:
        if s.name == ROOT_SPAN_NAME:
            return s.attrs or {}
    # Fall back to whatever span carries the graph module / thread id.
    for s in spans:
        a = s.attrs or {}
        if "husk.graph_module" in a or "husk.thread_id" in a:
            return a
    return {}


def _detect_recursion(spans: list[SpanRow], run_error: str | None) -> bool:
    needles = ("recursion", "graphrecursionerror")
    blobs: list[str] = [run_error or ""]
    for s in spans:
        if s.error_payload:
            blobs.append(str(s.error_payload.get("message") or ""))
    return any(any(n in b.lower() for n in needles) for b in blobs)


def build_run_graph(run: RunRow, spans: list[SpanRow]) -> RunGraph:
    """Reconstruct the per-node graph for a run from its spans."""
    # Group spans by husk.node.
    by_node: dict[str, list[SpanRow]] = {}
    for s in spans:
        node = (s.attrs or {}).get("husk.node")
        if not node:
            continue
        by_node.setdefault(str(node), []).append(s)

    root = _root_attrs(spans)
    topo_nodes = _loads(root.get("husk.graph.nodes")) or []
    topo_edges = _loads(root.get("husk.graph.edges")) or []
    cond_edges = _loads(root.get("husk.graph.conditional_edges")) or []

    nodes: list[NodeView] = []
    for name, group in by_node.items():
        wrapper = next((s for s in group if _is_wrapper(s)), None)
        workers = [s for s in group if s is not wrapper]
        attrs = (wrapper.attrs if wrapper else {}) or {}

        seq_raw = attrs.get("husk.node_seq")
        seq = int(seq_raw) if isinstance(seq_raw, (int, str)) and str(seq_raw).lstrip("-").isdigit() else None

        model_calls = [_model_call(s) for s in workers if s.kind == "llm"]
        tool_calls = [_tool_call(s) for s in workers if s.kind == "tool"]

        tokens_in = sum((s.tokens_in or 0) for s in workers)
        tokens_out = sum((s.tokens_out or 0) for s in workers)
        node_cost = sum(cost_usd(s.model, s.tokens_in, s.tokens_out) or 0.0 for s in workers)

        # Status: error if anything in the group errored; running if unfinished.
        errored = [s for s in group if s.status == "error"]
        unfinished = [s for s in group if s.finished_at is None]
        status = "error" if errored else ("running" if unfinished else "success")
        error_payload = next((s.error_payload for s in errored if s.error_payload), None)

        starts = [s.started_at for s in group if s.started_at is not None]
        ends = [s.finished_at for s in group if s.finished_at is not None]

        nodes.append(
            NodeView(
                id=name,
                name=name,
                seq=seq if seq is not None else 0,
                status=status,
                span_id=(wrapper or (workers[0] if workers else group[0])).id,
                started_at=min(starts) if starts else None,
                finished_at=max(ends) if ends else None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=round(node_cost, 6),
                is_failure_point=False,  # assigned below
                state_before=_loads(attrs.get("husk.state_before")),
                state_after=_loads(attrs.get("husk.state_after")),
                state_diff=_loads(attrs.get("husk.state_diff")),
                model_calls=model_calls,
                tool_calls=tool_calls,
                error=error_payload,
                traceback=attrs.get("husk.error.traceback"),
            )
        )

    # Order: by node_seq when present, else by start time.
    have_seq = all(n.seq is not None for n in nodes) and len(nodes) > 0
    nodes.sort(key=lambda n: (n.seq, n.started_at or 0) if have_seq else (n.started_at or 0, n.seq))
    # Backfill seq for stable indices.
    for i, n in enumerate(nodes):
        n.seq = i if not have_seq else n.seq

    executed_names = [n.id for n in nodes]

    # Add skipped topology nodes (in topology order) that never ran.
    if isinstance(topo_nodes, list) and topo_nodes:
        ran = set(executed_names)
        for idx, name in enumerate(topo_nodes):
            if name in ran:
                continue
            nodes.append(
                NodeView(
                    id=str(name),
                    name=str(name),
                    seq=1000 + idx,
                    status="skipped",
                    span_id=None,
                    started_at=None,
                    finished_at=None,
                    tokens_in=0,
                    tokens_out=0,
                    cost_usd=0.0,
                    is_failure_point=False,
                    state_before=None,
                    state_after=None,
                    state_diff=None,
                )
            )

    # Failure point: earliest errored node by executed order.
    failure: dict[str, Any] | None = None
    for n in nodes:
        if n.status == "error":
            n.is_failure_point = True
            failure = {
                "node_id": n.id,
                "seq": n.seq,
                "span_id": n.span_id,
                "message": (n.error or {}).get("message") if n.error else (run.error_message or None),
            }
            break

    # Edges: prefer topology; else chain the executed order.
    edges: list[dict[str, Any]] = []
    if isinstance(topo_edges, list) and topo_edges:
        for e in topo_edges:
            if isinstance(e, (list, tuple)) and len(e) == 2:
                edges.append({"from": str(e[0]), "to": str(e[1]), "conditional": False, "label": None})
            elif isinstance(e, dict) and "from" in e and "to" in e:
                edges.append({"from": str(e["from"]), "to": str(e["to"]), "conditional": False, "label": None})
    else:
        for a, b in zip(executed_names, executed_names[1:], strict=False):
            edges.append({"from": a, "to": b, "conditional": False, "label": None})
    if isinstance(cond_edges, list):
        for e in cond_edges:
            if isinstance(e, dict) and "from" in e and "to" in e:
                edges.append(
                    {
                        "from": str(e["from"]),
                        "to": str(e["to"]),
                        "conditional": True,
                        "label": e.get("label") or e.get("condition"),
                    }
                )

    return RunGraph(
        run_id=run.id,
        nodes=nodes,
        edges=edges,
        executed_path=executed_names,
        failure=failure,
        recursion_limit_hit=_detect_recursion(spans, run.error_message),
        run_status=run.status,
        run_error=run.error_message,
    )


# --- serialization for the API -------------------------------------------------


def _model_call_dict(m: ModelCall) -> dict[str, Any]:
    return {
        "span_id": m.span_id,
        "model": m.model,
        "provider": m.provider,
        "tokens_in": m.tokens_in,
        "tokens_out": m.tokens_out,
        "prompt": m.prompt,
        "response": m.response,
        "status": m.status,
        "error": m.error,
    }


def _tool_call_dict(t: ToolCall) -> dict[str, Any]:
    return {
        "span_id": t.span_id,
        "name": t.name,
        "args": t.args,
        "result": t.result,
        "status": t.status,
        "error": t.error,
    }


def node_dict(n: NodeView) -> dict[str, Any]:
    return {
        "id": n.id,
        "name": n.name,
        "seq": n.seq,
        "status": n.status,
        "span_id": n.span_id,
        "started_at": n.started_at,
        "finished_at": n.finished_at,
        "duration_ms": n.duration_ms,
        "tokens_in": n.tokens_in,
        "tokens_out": n.tokens_out,
        "cost_usd": n.cost_usd,
        "is_failure_point": n.is_failure_point,
        "state_before": n.state_before,
        "state_after": n.state_after,
        "state_diff": n.state_diff,
        "model_calls": [_model_call_dict(m) for m in n.model_calls],
        "tool_calls": [_tool_call_dict(t) for t in n.tool_calls],
        "error": n.error,
        "traceback": n.traceback,
    }


def run_graph_dict(g: RunGraph) -> dict[str, Any]:
    return {
        "run_id": g.run_id,
        "nodes": [node_dict(n) for n in g.nodes],
        "edges": g.edges,
        "executed_path": g.executed_path,
        "failure": g.failure,
        "recursion_limit_hit": g.recursion_limit_hit,
        "run_status": g.run_status,
        "run_error": g.run_error,
    }
