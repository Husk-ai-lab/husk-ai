"""Assemble a failure-focused debug context for the LLM, within a token budget.

Big traces overflow the context window, so we never send everything raw. The
region around the failure is sent at full fidelity; far regions are summarized or
(only if still over budget) dropped — noise far from the failure is cut first,
the failure region never is. The budget is the selected model's context window
(``husk_studio_shared.model_metadata``) minus the model's own output reserve and the
system prompt. Token counts use a cheap chars/4 heuristic (no extra deps).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from husk_studio_backend.db.models import RunRow, SpanRow
from husk_studio_backend.debugger.system_prompt import DEBUGGER_SYSTEM_PROMPT
from husk_studio_backend.graph_model import NodeView, RunGraph, build_run_graph, node_dict
from husk_studio_shared.model_metadata import context_window, max_output

log = logging.getLogger(__name__)

WINDOW_RADIUS = 2  # nodes of full detail on each side of the failure
_STRING_TRUNC = 1200  # chars kept per long string under last-resort truncation
_PREVIEW = 240  # chars in a far-node output preview
_SCAFFOLD_RESERVE = 2000  # tokens reserved for JSON scaffolding/instructions
_SPANS_CAP = 120  # observability-only fallback: cap raw steps sent


def estimate_tokens(obj: Any) -> int:
    """Cheap, dependency-free token estimate: ~4 chars per token."""
    if isinstance(obj, str):
        return len(obj) // 4 + 1
    return len(json.dumps(obj, default=str)) // 4 + 1


def _truncate_strings(obj: Any, limit: int = _STRING_TRUNC) -> Any:
    if isinstance(obj, str):
        if len(obj) <= limit:
            return obj
        head = obj[: limit // 2]
        tail = obj[-limit // 2 :]
        return f"{head}\n…[{len(obj) - limit} chars elided]…\n{tail}"
    if isinstance(obj, list):
        return [_truncate_strings(x, limit) for x in obj]
    if isinstance(obj, dict):
        return {k: _truncate_strings(v, limit) for k, v in obj.items()}
    return obj


def _diff_summary(diff: Any) -> Any:
    """Keep only which keys changed, not their (possibly huge) values."""
    if not isinstance(diff, dict):
        return None
    return {
        "added": sorted((diff.get("added") or {}).keys()),
        "removed": sorted((diff.get("removed") or {}).keys()),
        "changed": sorted((diff.get("changed") or {}).keys()),
    }


def _preview(value: Any) -> str | None:
    if value is None:
        return None
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s[:_PREVIEW] + ("…" if len(s) > _PREVIEW else "")


def _summary_node(n: NodeView) -> dict[str, Any]:
    last_response = n.model_calls[-1].response if n.model_calls else None
    return {
        "id": n.id,
        "seq": n.seq,
        "status": n.status,
        "tokens_in": n.tokens_in,
        "tokens_out": n.tokens_out,
        "state_diff_keys": _diff_summary(n.state_diff),
        "output_preview": _preview(last_response),
        "summarized": True,
    }


def _full_node(n: NodeView) -> dict[str, Any]:
    d = node_dict(n)
    d["summarized"] = False
    return d


def _failure_index(g: RunGraph) -> int:
    if g.failure is not None:
        for i, n in enumerate(g.nodes):
            if n.id == g.failure["node_id"]:
                return i
    # No explicit failure: anchor on the last executed node (a "wrong result" run).
    executed = [i for i, n in enumerate(g.nodes) if n.status != "skipped"]
    return executed[-1] if executed else 0


def read_source_for_run(run: RunRow, spans: list[SpanRow]) -> tuple[str | None, str | None]:
    """Best-effort read of the agent's own source file (local-first).

    Returns (source_text, path). The graph module attr is ``<path>:<symbol>``;
    rpartition on the last colon handles Windows drive letters (``C:\\...``).
    """
    module: str | None = None
    meta = run.extra_metadata if isinstance(run.extra_metadata, dict) else {}
    module = meta.get("husk.graph_module")
    if not module:
        for s in spans:
            m = (s.attrs or {}).get("husk.graph_module")
            if m:
                module = str(m)
                break
    if not module:
        return None, None
    path_str, _, _ = module.rpartition(":")
    path_str = path_str or module
    try:
        text = Path(path_str).read_text(encoding="utf-8")
        return text, path_str
    except OSError:
        return None, path_str


def _fit_source(text: str | None, path: str | None, cap_tokens: int) -> tuple[dict[str, Any] | None, bool]:
    if not text:
        return (None, False)
    if estimate_tokens(text) <= cap_tokens:
        return ({"path": path, "code": text, "truncated": False}, False)
    # Keep head + tail to preserve imports and the likely-edited region.
    keep = cap_tokens * 4
    head = text[: keep // 2]
    tail = text[-keep // 2 :]
    body = f"{head}\n# …[source truncated to fit context]…\n{tail}"
    return ({"path": path, "code": body, "truncated": True}, True)


def _span_node(sp: SpanRow, seq: int) -> dict[str, Any]:
    err = sp.error_payload
    return {
        "id": sp.name or sp.id,
        "seq": seq,
        "kind": sp.kind,
        "status": sp.status,
        "model": sp.model,
        "provider": sp.provider,
        "tokens_in": sp.tokens_in,
        "tokens_out": sp.tokens_out,
        "cost_usd": sp.cost_usd,
        "input_preview": _preview(sp.input_inline),
        "output_preview": _preview(sp.output_inline),
        "error": _truncate_strings(err) if err else None,
    }


def _spans_context(
    run: RunRow, spans: list[SpanRow], *, model: str, include_source: bool
) -> dict[str, Any]:
    """Fallback context for observability-only runs (OTel ingest, no husk graph).

    No node graph or state diff exists, so hand the model the raw steps (LLM/tool
    calls) with model, tokens, status, and any error, anchored on the failing
    spans. Marked ``observability_only`` so the prompt lowers its confidence."""
    budget = context_window(model)
    reserve = estimate_tokens(DEBUGGER_SYSTEM_PROMPT) + _SCAFFOLD_RESERVE
    input_budget = max(2000, budget - max_output(model) - reserve)

    ordered = sorted(spans, key=lambda s: s.started_at or 0)[:_SPANS_CAP]
    nodes = [_span_node(sp, i) for i, sp in enumerate(ordered)]
    failing = [nd["id"] for nd in nodes if nd["status"] == "error" or nd["error"]]

    failure: dict[str, Any] | None = None
    if failing or run.error_message:
        failure = {"run_error": run.error_message, "failing_spans": failing}

    context: dict[str, Any] = {
        "run_id": run.id,
        "run_status": run.status,
        "mode": "observability_only",
        "spans": nodes,
        "failure": failure,
    }
    if include_source:
        text, path = read_source_for_run(run, spans)
        block, _ = _fit_source(text, path, int(input_budget * 0.35))
        if block:
            context["source"] = block

    # Crude budget fit: truncate long strings, then drop previews on non-failing
    # spans. Observability-only runs rarely overflow.
    if estimate_tokens(context) > input_budget:
        context["spans"] = _truncate_strings(context["spans"])
    if estimate_tokens(context) > input_budget:
        for nd in context["spans"]:
            if nd["id"] not in failing:
                nd.pop("input_preview", None)
                nd.pop("output_preview", None)
        context["truncation"] = {"applied": True, "string_truncation": True}

    context["_assembled_token_estimate"] = estimate_tokens(context)
    return context


def build_debug_context(
    run: RunRow,
    spans: list[SpanRow],
    *,
    model: str,
    include_source: bool = True,
) -> dict[str, Any]:
    """Build the JSON object the debugger system prompt consumes, within budget."""
    g = build_run_graph(run, spans)
    if not g.nodes:
        # Observability-only run (OTel ingest, no husk graph instrumentation):
        # fall back to a span-level context so the debugger can still localize.
        return _spans_context(run, spans, model=model, include_source=include_source)
    n = len(g.nodes)
    f = _failure_index(g)

    budget = context_window(model)
    reserve = estimate_tokens(DEBUGGER_SYSTEM_PROMPT) + _SCAFFOLD_RESERVE
    input_budget = max(2000, budget - max_output(model) - reserve)

    # Source (optional, capped).
    source_block: dict[str, Any] | None = None
    source_truncated = False
    if include_source:
        text, path = read_source_for_run(run, spans)
        source_block, source_truncated = _fit_source(text, path, int(input_budget * 0.35))
    source_tokens = estimate_tokens(source_block) if source_block else 0

    base = {
        "graph": {
            "nodes": [{"id": nd.id, "status": nd.status, "seq": nd.seq} for nd in g.nodes],
            "edges": g.edges,
            "executed_path": g.executed_path,
        },
        "failure": _failure_block(g),
    }
    avail = input_budget - source_tokens - estimate_tokens(base)

    # Representation per node: window = full, others = summary, then adjust to fit.
    window = set(range(max(0, f - WINDOW_RADIUS), min(n, f + 2)))
    rep: dict[int, str] = {i: ("full" if i in window else "summary") for i in range(n)}

    def render_nodes() -> tuple[list[dict[str, Any]], list[str]]:
        payload: list[dict[str, Any]] = []
        dropped: list[str] = []
        for i, node in enumerate(g.nodes):
            mode = rep[i]
            if mode == "full":
                payload.append(_full_node(node))
            elif mode == "summary":
                payload.append(_summary_node(node))
            else:
                dropped.append(node.id)
        return payload, dropped

    def cost() -> int:
        payload, _ = render_nodes()
        return estimate_tokens(payload)

    truncation: dict[str, Any] = {
        "applied": False,
        "summarized_regions": [],
        "dropped_regions": [],
        "string_truncation": False,
        "source_truncated": source_truncated,
    }

    # Over budget: drop farthest non-window summaries first (noise, not failure).
    if cost() > avail:
        order = sorted(
            (i for i in range(n) if i not in window),
            key=lambda i: -abs(i - f),  # farthest from failure first
        )
        for i in order:
            if cost() <= avail:
                break
            rep[i] = "dropped"
        truncation["applied"] = True

    # Still over (window alone too big): last-resort string truncation in full nodes.
    if cost() > avail:
        truncation["string_truncation"] = True
        truncation["applied"] = True
        # Re-render full nodes with truncated strings by wrapping the renderer.
        full_truncate = True
    else:
        full_truncate = False

    # Under budget: greedily upgrade nearest summaries to full.
    if not full_truncate:
        candidates = sorted(
            (i for i in range(n) if rep[i] == "summary"),
            key=lambda i: abs(i - f),  # nearest to failure first
        )
        for i in candidates:
            rep[i] = "full"
            if cost() > avail:
                rep[i] = "summary"

    # Final node payload (apply string truncation to full nodes if needed).
    node_payload: list[dict[str, Any]] = []
    dropped: list[str] = []
    for i, node in enumerate(g.nodes):
        mode = rep[i]
        if mode == "full":
            d = _full_node(node)
            if full_truncate and not node.is_failure_point:
                d = _truncate_strings(d)
            node_payload.append(d)
        elif mode == "summary":
            node_payload.append(_summary_node(node))
            truncation["summarized_regions"].append(node.id)
        else:
            dropped.append(node.id)
    truncation["dropped_regions"] = dropped
    if truncation["summarized_regions"] or dropped or truncation["string_truncation"]:
        truncation["applied"] = True

    context: dict[str, Any] = {
        "run_id": run.id,
        "run_status": g.run_status,
        "graph": base["graph"],
        "executed_path": g.executed_path,
        "nodes": node_payload,
        "failure": base["failure"],
        "recursion_limit_hit": g.recursion_limit_hit,
    }
    if source_block:
        context["source"] = source_block
    if truncation["applied"]:
        context["truncation"] = truncation
    context["_assembled_token_estimate"] = estimate_tokens(context)
    return context


def _failure_block(g: RunGraph) -> dict[str, Any] | None:
    if g.failure is None and not g.run_error:
        return None
    block: dict[str, Any] = dict(g.failure or {})
    block["run_error"] = g.run_error
    block["recursion_limit_hit"] = g.recursion_limit_hit
    # Attach the failing node's traceback if we captured one.
    if g.failure is not None:
        node = next((n for n in g.nodes if n.id == g.failure.get("node_id")), None)
        if node and node.traceback:
            block["traceback"] = node.traceback
    return block
