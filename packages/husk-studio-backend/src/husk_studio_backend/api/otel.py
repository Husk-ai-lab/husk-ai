"""OTLP/HTTP ingestion endpoint for OTel GenAI traces.

Accepts:
- Content-Type: application/json     (OTLP/HTTP JSON)
- Content-Type: application/x-protobuf (OTLP/HTTP proto)

Spans are mapped to Husk Run/Span rows via `ingest.otel_parser` and streamed
to WebSocket subscribers via `ingest.broadcast.publish`.

Development-only by design: this route is mounted behind the loopback ``local_only``
guard (see ``main``), so ONLY an agent running on this machine can stream traces in. A
production deployment on another host is refused at the door. Husk is a tool you use
while building an agent, before it ships — it is not, and cannot be, production
monitoring.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from husk_studio_backend.db.engine import async_session
from husk_studio_backend.db.models import RunRow, SpanRow
from husk_studio_backend.ingest.broadcast import publish
from husk_studio_backend.ingest.otel_parser import ParsedSpan, parse_otlp_traces
from husk_studio_shared.pricing import cost_usd

log = logging.getLogger(__name__)
router = APIRouter(tags=["otel"])

_OK_BODY = b'{"partialSuccess":{}}'


@router.post("/v1/traces", status_code=200)
async def ingest_traces(request: Request) -> Response:
    content_type = (request.headers.get("content-type") or "").lower()
    body = await request.body()

    if "json" in content_type:
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"invalid json: {e}") from e
    elif "protobuf" in content_type or "x-protobuf" in content_type:
        try:
            from google.protobuf import json_format
            from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            payload = json_format.MessageToDict(req, preserving_proto_field_name=False)
        except Exception as e:  # noqa: BLE001
            log.exception("OTLP proto decode failed")
            raise HTTPException(
                status_code=400, detail=f"proto decode failed: {e}"
            ) from e
    else:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content-type (need json or x-protobuf): {content_type!r}",
        )

    spans = parse_otlp_traces(payload)
    if not spans:
        return Response(content=_OK_BODY, media_type="application/json")

    # Group by run_id (= derived from trace_id).
    by_run: dict[str, list[ParsedSpan]] = {}
    for s in spans:
        by_run.setdefault(s.run_id, []).append(s)

    persisted_for_broadcast: list[tuple[str, dict[str, Any]]] = []
    # Local-first single-user: runs aren't scoped to a project, so this stays None.
    # The column is kept nullable only for forward-compat with the enterprise edition.
    project_id = getattr(request.state, "project_id", None)

    async with async_session() as session:
        # One query for every span id in the batch, instead of a SELECT per span.
        # OTel's BatchSpanProcessor exports up to 512 spans at a time, so the
        # per-span `session.get` this replaces meant ~512 round trips per export
        # on the hottest write path in the product.
        all_ids = [s.id for s in spans]
        existing_ids: set[str] = set()
        for chunk in _chunks(all_ids, 500):  # stay under SQLite's variable limit
            found = (
                await session.execute(select(SpanRow.id).where(SpanRow.id.in_(chunk)))
            ).scalars().all()
            existing_ids.update(found)

        for run_id, run_spans in by_run.items():
            await _upsert_run(session, run_id, run_spans, project_id)
            for s in run_spans:
                added = await _upsert_span(session, s, known_existing=s.id in existing_ids)
                if added:
                    persisted_for_broadcast.append((s.run_id, _serialize(s)))
        await session.commit()

    # Broadcast new spans to WebSocket subscribers (after commit).
    for run_id, span_payload in persisted_for_broadcast:
        await publish(
            run_id,
            {"type": "span.created", "run_id": run_id, "span": span_payload},
        )

    # Optional auto-debug for failed runs (off by default; no-op without a key).
    from husk_studio_backend.debugger.autostart import maybe_autostart

    for run_id in by_run:
        await maybe_autostart(run_id)

    return Response(content=_OK_BODY, media_type="application/json")


async def _upsert_run(
    session: AsyncSession,
    run_id: str,
    spans: list[ParsedSpan],
    project_id: str | None = None,
) -> None:
    row = await session.get(RunRow, run_id)
    earliest = min(spans, key=lambda s: s.started_at_ms or 0)
    framework_id = earliest.gen_ai_system or earliest.service_name or "otel"
    framework_label = (
        f"otel/{framework_id}" if not framework_id.startswith("otel") else framework_id
    )

    if row is None:
        row = RunRow(
            id=run_id,
            script_path=earliest.service_name or "",
            framework=framework_label,
            status="running",
            started_at=earliest.started_at_ms,
            # Real indexed column; the dashboard's project switcher filters on it.
            project_id=project_id,
        )
        session.add(row)
    elif project_id and not row.project_id:
        row.project_id = project_id

    # Roll forward finish time + status.
    #
    # A run is only complete when its ROOT span closes. Treating "any span finished"
    # as "run finished" reports success while the agent is still working: OTLP
    # arrives in batches, and the first batch normally carries finished child spans
    # under a root that is still open. That premature `success` also makes the
    # Studio skip the live WebSocket for the run, freezing the timeline mid-flight.
    #
    # This works with OTel's export order rather than against it: a span is exported
    # when it ends, so the root ends last and its arrival IS the completion signal.
    # While the agent is mid-flight the root simply hasn't been exported yet.
    root_finish = max(
        (s.finished_at_ms or 0 for s in spans if s.parent_span_id is None),
        default=0,
    )

    latest_finish = max((s.finished_at_ms or 0) for s in spans)
    if latest_finish and (row.finished_at is None or latest_finish > row.finished_at):
        row.finished_at = latest_finish
    if any(s.status == "error" for s in spans):
        row.status = "error"
    elif root_finish and row.status == "running":
        row.status = "success"


async def _upsert_span(
    session: AsyncSession, s: ParsedSpan, *, known_existing: bool | None = None
) -> bool:
    """Insert a new span or update terminal fields. Returns True if newly inserted.

    ``known_existing`` comes from the caller's single bulk id lookup; when it is
    False we skip the per-span SELECT entirely.
    """
    existing = None if known_existing is False else await session.get(SpanRow, s.id)
    cost = cost_usd(s.model, s.tokens_in, s.tokens_out)

    if existing is None:
        session.add(
            SpanRow(
                id=s.id,
                run_id=s.run_id,
                parent_span_id=s.parent_span_id,
                kind=s.kind,
                name=s.name,
                started_at=s.started_at_ms,
                finished_at=s.finished_at_ms,
                status=s.status,
                input_inline=s.input_inline,
                output_inline=s.output_inline,
                tokens_in=s.tokens_in,
                tokens_out=s.tokens_out,
                cost_usd=cost,
                provider=s.provider,
                model=s.model,
                error_payload=s.error_payload,
                attrs={k: v for k, v in s.attrs.items() if k != "_resource"},
            )
        )
        run = await session.get(RunRow, s.run_id)
        if run is not None:
            if s.tokens_in:
                run.total_tokens_in = (run.total_tokens_in or 0) + s.tokens_in
            if s.tokens_out:
                run.total_tokens_out = (run.total_tokens_out or 0) + s.tokens_out
            if cost:
                run.total_cost_usd = (run.total_cost_usd or 0.0) + cost
        return True

    # Span already exists — update terminal fields opportunistically.
    if existing.finished_at is None and s.finished_at_ms:
        existing.finished_at = s.finished_at_ms
    if s.status:
        existing.status = s.status
    if existing.output_inline is None and s.output_inline:
        existing.output_inline = s.output_inline

    # Usage often lands on a later export of the same span (streaming LLM spans
    # end before their token counts are known). Without this the counts are
    # dropped for good and every run under-reports its tokens and cost. Apply the
    # delta to the run totals so re-exports stay idempotent rather than double-counting.
    run = None
    for field, incoming in (("tokens_in", s.tokens_in), ("tokens_out", s.tokens_out)):
        if incoming is None or incoming == (getattr(existing, field) or 0):
            continue
        delta = incoming - (getattr(existing, field) or 0)
        setattr(existing, field, incoming)
        run = run or await session.get(RunRow, s.run_id)
        if run is not None:
            total = f"total_{field}"
            setattr(run, total, max(0, (getattr(run, total) or 0) + delta))

    if cost is not None and cost != (existing.cost_usd or 0.0):
        cost_delta = cost - (existing.cost_usd or 0.0)
        existing.cost_usd = cost
        run = run or await session.get(RunRow, s.run_id)
        if run is not None:
            run.total_cost_usd = max(0.0, (run.total_cost_usd or 0.0) + cost_delta)
    return False


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _serialize(s: ParsedSpan) -> dict[str, Any]:
    return {
        "id": s.id,
        "run_id": s.run_id,
        "parent_span_id": s.parent_span_id,
        "kind": s.kind,
        "name": s.name,
        "started_at": s.started_at_ms,
        "finished_at": s.finished_at_ms,
        "status": s.status,
        "input_inline": s.input_inline,
        "output_inline": s.output_inline,
        "tokens_in": s.tokens_in,
        "tokens_out": s.tokens_out,
        "cost_usd": cost_usd(s.model, s.tokens_in, s.tokens_out),
        "provider": s.provider,
        "model": s.model,
        "error_payload": s.error_payload,
        "attrs": {k: v for k, v in s.attrs.items() if k != "_resource"},
    }
