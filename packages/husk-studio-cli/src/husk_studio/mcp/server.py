"""The `husk-studio mcp` server.

Read/introspection tools query the local SQLite DB (`~/.husk/traces.db`) directly
through the existing ORM, so they work whether or not `husk-studio start` is running.
The single action tool, `replay_run`, calls the backend's HTTP API (replay needs
the live backend for the OTel round-trip + dynamic import) and is only registered
when replay is explicitly enabled — it executes your agent code, so it is
local-only by design.

Serializers mirror the backend's REST output shape (see api/runs.py, api/spans.py)
rather than the strict `husk_studio_shared.schemas` models — the stored `framework`/
`kind`/`status` strings are free-form (e.g. "otel/openai") and would trip the
schemas' enums, and SQLAlchemy's declarative `.metadata` collides with the Run
schema's `metadata` field. Building dicts directly keeps reads robust.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy import func, select

from husk_studio_backend.db.engine import async_session, init_db
from husk_studio_backend.db.models import CursorEventRow, RunRow, SpanRow
from husk_studio_shared.pricing import cost_usd as _price

# Default port for the HTTP transport. The backend owns 7654; the MCP server
# takes the next port so both can run side by side.
DEFAULT_HTTP_PORT = 7655

_REPLAY_ENV = "HUSK_MCP_ENABLE_REPLAY"

INSTRUCTIONS = """\
Husk is a local visual debugger for AI agents. These tools read your local Husk
database of agent runs (captured via OpenTelemetry instrumentation and IDE hooks)
and let you replay them.

Typical flow:
  - `list_runs` / `list_errors` to find a run (newest first).
  - `get_trace(run_id)` to see what the agent did, step by step (the span tree).
  - `get_span(run_id, span_id)` for the full, untruncated input/output of one step.
  - `cost_breakdown` for token and USD usage by model.
  - `replay_run(run_id, state_override=...)` to re-run a recorded graph from its
    checkpoint with modified state (only available when the operator enabled it).

A "run" is one agent execution; a "span" is one step within it (an LLM call, a
tool call, a graph node). Timestamps are Unix milliseconds.
"""


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def husk_url() -> str:
    """Base URL of the running Husk backend (matches the IDE hooks' HUSK_URL)."""
    return os.environ.get("HUSK_URL", "http://127.0.0.1:7654").rstrip("/")


def _truncate_io(value: Any, max_chars: int | None) -> Any:
    """Replace an oversized inline input/output with a short pointer.

    Keeps trace listings cheap to read; the model can always call `get_span` for
    the full content.
    """
    if value is None or max_chars is None:
        return value
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= max_chars:
        return value
    return f"<{len(rendered)} chars truncated — call get_span(run_id, span_id) for full content>"


def _run_to_dict(row: RunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "parent_run_id": row.parent_run_id,
        "fork_span_id": row.fork_span_id,
        "script_path": row.script_path,
        "script_argv": row.script_argv or [],
        "framework": row.framework,
        "status": row.status,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration_ms": (row.finished_at - row.started_at) if row.finished_at else None,
        "total_tokens_in": row.total_tokens_in,
        "total_tokens_out": row.total_tokens_out,
        "total_cost_usd": row.total_cost_usd,
        "env_fingerprint": row.env_fingerprint,
        "error_message": row.error_message,
        "metadata": row.extra_metadata or {},
    }


def _span_to_dict(
    row: SpanRow, *, include_io: bool = True, max_io_chars: int | None = None
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": row.id,
        "run_id": row.run_id,
        "parent_span_id": row.parent_span_id,
        "kind": row.kind,
        "name": row.name,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "duration_ms": (row.finished_at - row.started_at) if row.finished_at else None,
        "status": row.status,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_usd": row.cost_usd,
        "provider": row.provider,
        "model": row.model,
        "error_payload": row.error_payload,
        "attrs": row.attrs or {},
    }
    if include_io:
        d["input_inline"] = _truncate_io(row.input_inline, max_io_chars)
        d["output_inline"] = _truncate_io(row.output_inline, max_io_chars)
    return d


def build_server(
    *,
    enable_replay: bool = False,
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
) -> FastMCP:
    """Construct the Husk FastMCP server with all tools registered.

    `replay_run` is registered only when `enable_replay` is true OR the
    HUSK_MCP_ENABLE_REPLAY env var is set — it runs your agent code.
    """
    enable_replay = enable_replay or _env_truthy(_REPLAY_ENV)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> Any:
        # Ensure the tables exist even if `husk-studio start` has never run. init_db
        # is idempotent (CREATE TABLE IF NOT EXISTS via create_all) and runs in
        # the server's own event loop, so the cached async engine binds here.
        await init_db()
        yield {}

    mcp = FastMCP(
        "husk",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        lifespan=lifespan,
    )

    @mcp.tool()
    async def list_runs(
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
        framework: str | None = None,
    ) -> list[dict[str, Any]]:
        """List recent agent runs, newest first.

        Optionally filter by `status` (pending|running|success|error|aborted) or
        `framework` (e.g. "otel/openai", "unknown").
        """
        async with async_session() as s:
            q = select(RunRow).order_by(RunRow.started_at.desc())
            if status:
                q = q.where(RunRow.status == status)
            if framework:
                q = q.where(RunRow.framework == framework)
            rows = (await s.execute(q.limit(limit).offset(offset))).scalars().all()
        return [_run_to_dict(r) for r in rows]

    @mcp.tool()
    async def get_run(run_id: str) -> dict[str, Any]:
        """Get one run's metadata and rollup (status, timing, tokens, cost, error)."""
        async with async_session() as s:
            row = await s.get(RunRow, run_id)
            if row is None:
                return {"error": f"run {run_id!r} not found"}
            return _run_to_dict(row)

    @mcp.tool()
    async def get_trace(
        run_id: str, include_io: bool = True, max_io_chars: int = 2000
    ) -> dict[str, Any]:
        """Get the full span tree for a run — what the agent did, step by step.

        Returns the run summary plus `spans`, a hierarchy nested by
        `parent_span_id`. Large inline inputs/outputs are truncated to
        `max_io_chars`; set `include_io=False` to drop them, or call `get_span`
        for one step's full content.
        """
        async with async_session() as s:
            run = await s.get(RunRow, run_id)
            if run is None:
                return {"error": f"run {run_id!r} not found"}
            rows = (
                (
                    await s.execute(
                        select(SpanRow)
                        .where(SpanRow.run_id == run_id)
                        .order_by(SpanRow.started_at.asc())
                    )
                )
                .scalars()
                .all()
            )

        nodes: dict[str, dict[str, Any]] = {}
        for r in rows:
            node = _span_to_dict(
                r, include_io=include_io, max_io_chars=max_io_chars if include_io else None
            )
            node["children"] = []
            nodes[r.id] = node

        roots: list[dict[str, Any]] = []
        for r in rows:
            node = nodes[r.id]
            parent = nodes.get(r.parent_span_id) if r.parent_span_id else None
            if parent is not None and parent is not node:
                parent["children"].append(node)
            else:
                roots.append(node)

        return {"run": _run_to_dict(run), "spans": roots, "span_count": len(rows)}

    @mcp.tool()
    async def get_span(run_id: str, span_id: str) -> dict[str, Any]:
        """Get one span in full detail (untruncated input/output, error, attrs)."""
        async with async_session() as s:
            row = await s.get(SpanRow, span_id)
            if row is None or row.run_id != run_id:
                return {"error": f"span {span_id!r} not found in run {run_id!r}"}
            return _span_to_dict(row, include_io=True, max_io_chars=None)

    @mcp.tool()
    async def list_errors(limit: int = 20) -> dict[str, Any]:
        """Triage failures: recent failed runs and failed spans (with error payloads)."""
        async with async_session() as s:
            failed_runs = (
                (
                    await s.execute(
                        select(RunRow)
                        .where(RunRow.status == "error")
                        .order_by(RunRow.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            failed_spans = (
                (
                    await s.execute(
                        select(SpanRow)
                        .where(SpanRow.status == "error")
                        .order_by(SpanRow.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "failed_runs": [_run_to_dict(r) for r in failed_runs],
            "failed_spans": [
                _span_to_dict(r, include_io=True, max_io_chars=2000)
                for r in failed_spans
            ],
        }

    @mcp.tool()
    async def cost_breakdown(run_id: str | None = None) -> dict[str, Any]:
        """Token and USD cost grouped by provider+model. Pass `run_id` to scope to one run.

        Uses each span's stored cost; when a span has no stored cost but a known
        model, the cost is estimated from Husk's static pricing table.
        """
        async with async_session() as s:
            q = select(
                SpanRow.provider,
                SpanRow.model,
                func.count(SpanRow.id),
                func.coalesce(func.sum(SpanRow.tokens_in), 0),
                func.coalesce(func.sum(SpanRow.tokens_out), 0),
                func.coalesce(func.sum(SpanRow.cost_usd), 0.0),
            ).group_by(SpanRow.provider, SpanRow.model)
            if run_id:
                q = q.where(SpanRow.run_id == run_id)
            rows = (await s.execute(q)).all()

        by_model: list[dict[str, Any]] = []
        tot_in = tot_out = 0
        tot_cost = 0.0
        for provider, model, calls, tin, tout, cost in rows:
            estimated = False
            cost = float(cost or 0.0)
            if cost == 0.0 and model:
                est = _price(model, int(tin or 0), int(tout or 0))
                if est:
                    cost = est
                    estimated = True
            tot_in += int(tin or 0)
            tot_out += int(tout or 0)
            tot_cost += cost
            by_model.append(
                {
                    "provider": provider,
                    "model": model,
                    "calls": int(calls or 0),
                    "tokens_in": int(tin or 0),
                    "tokens_out": int(tout or 0),
                    "cost_usd": round(cost, 6),
                    "estimated": estimated,
                }
            )
        by_model.sort(key=lambda r: r["cost_usd"], reverse=True)
        return {
            "scope": run_id or "all runs",
            "by_model": by_model,
            "totals": {
                "tokens_in": tot_in,
                "tokens_out": tot_out,
                "cost_usd": round(tot_cost, 6),
            },
        }

    @mcp.tool()
    async def dashboard_summary() -> dict[str, Any]:
        """Aggregate stats across all runs (totals, last 24h, by framework, recent runs)."""
        # Lazy import keeps FastAPI off the import path for the read tools above.
        from husk_studio_backend.api.dashboard import compute_dashboard_summary

        result: dict[str, Any] = await compute_dashboard_summary()
        return result

    @mcp.tool()
    async def list_cursor_events(
        limit: int = 20, project: str | None = None
    ) -> list[dict[str, Any]]:
        """List recent IDE observability events (file edits, stop signals), newest first."""
        async with async_session() as s:
            q = select(CursorEventRow).order_by(CursorEventRow.created_at.desc())
            if project:
                q = q.where(CursorEventRow.project == project)
            rows = (await s.execute(q.limit(limit))).scalars().all()
        return [
            {
                "id": r.id,
                "hook": r.hook,
                "project": r.project,
                "payload": r.payload,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    if enable_replay:

        @mcp.tool()
        async def replay_run(
            run_id: str,
            state_override: dict[str, Any] | None = None,
            span_id: str | None = None,
            env_overrides: dict[str, str] | None = None,
            fork_node: str | None = None,
            parent_thread_id: str | None = None,
        ) -> dict[str, Any]:
            """Re-invoke a recorded graph run from its checkpoint with modified state.

            Requires the Husk backend to be running (`husk-studio start`). Executes
            your agent code locally. `state_override` is the new initial state
            (e.g. {"messages": [...]}); pass {} for a plain re-run. Returns the
            new thread_id — the resulting run appears in `list_runs` once OTel
            flushes (~1s).
            """
            import httpx

            body: dict[str, Any] = {
                "run_id": run_id,
                "span_id": span_id,
                "state_override": state_override or {},
                "env_overrides": env_overrides,
                "fork_node": fork_node,
                "parent_thread_id": parent_thread_id,
            }
            url = f"{husk_url()}/api/replay"
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(url, json=body)
            except httpx.ConnectError:
                return {
                    "error": (
                        f"Husk Studio backend not reachable at {husk_url()}. "
                        "Start it first: `husk-studio start` (or set HUSK_URL)."
                    )
                }
            if resp.status_code >= 400:
                return {
                    "error": f"replay failed ({resp.status_code})",
                    "detail": resp.text,
                }
            payload: dict[str, Any] = resp.json()
            return payload

    return mcp


def serve(
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = DEFAULT_HTTP_PORT,
    enable_replay: bool = False,
) -> None:
    """Build and run the server on the given transport (blocks).

    `transport`: "stdio" (default; for Claude Code, Cursor, Claude Desktop,
    Windsurf), or "http"/"sse" (for remote clients like Lovable behind a tunnel).
    """
    server = build_server(enable_replay=enable_replay, host=host, port=port)
    # FastMCP expects a Literal; map our friendly "http" alias and branch so the
    # type checker sees concrete literals.
    if transport in ("http", "streamable-http"):
        server.run("streamable-http")
    elif transport == "sse":
        server.run("sse")
    else:
        server.run("stdio")
