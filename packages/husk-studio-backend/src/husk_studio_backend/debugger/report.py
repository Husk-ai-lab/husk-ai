"""Orchestrate one debug analysis: assemble context, call the provider, persist.

Flow: load run + spans → build the failure-focused context → resolve the local
key → call the provider (off the event loop) → strictly parse the JSON report →
persist a ``DebugReportRow``. The API key is read at call time and never logged,
never stored anywhere but ``secrets.json``, and never written into the report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy import select

from husk_studio_backend.db.engine import async_session
from husk_studio_backend.db.models import DebugReportRow, RunRow, SpanRow
from husk_studio_backend.debugger import providers, secrets
from husk_studio_backend.debugger.context_assembler import build_debug_context
from husk_studio_backend.debugger.schemas import DebugReport, extract_report
from husk_studio_backend.debugger.system_prompt import (
    DEBUGGER_SYSTEM_PROMPT,
    SYSTEM_PROMPT_VERSION,
)

log = logging.getLogger(__name__)


class DebuggerError(RuntimeError):
    """A recoverable debugger failure (missing key, provider error, bad output)."""


def _new_id() -> str:
    return uuid.uuid4().hex[:26]


def serialize_report_row(row: DebugReportRow) -> dict[str, Any]:
    """API shape for a persisted report. Never contains the key."""
    return {
        "id": row.id,
        "run_id": row.run_id,
        "report": row.report_json,
        "provider": row.provider,
        "model": row.model,
        "failure_node": row.failure_node,
        "failure_class": row.failure_class,
        "confidence": row.confidence,
        "system_prompt_version": row.system_prompt_version,
        "trigger": row.trigger,
        "applied": row.applied,
        "created_at": row.created_at,
    }


async def _load_run(run_id: str) -> tuple[RunRow, list[SpanRow]]:
    async with async_session() as s:
        run = await s.get(RunRow, run_id)
        if run is None:
            raise DebuggerError(f"run {run_id} not found")
        spans = (
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
        return run, list(spans)


async def run_debug_analysis(
    run_id: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Analyze a run and persist the report. Returns the serialized report row."""
    cfg = secrets.public_config()
    provider_name = provider or cfg["provider"]
    model_name = model or cfg["model"]

    api_key = secrets.resolve_api_key(provider_name)
    if not api_key:
        raise DebuggerError(
            f"no API key configured for provider {provider_name!r}; set one in Settings"
        )

    run, spans = await _load_run(run_id)
    context = build_debug_context(run, spans, model=model_name)
    user_content = json.dumps(context, default=str)

    prov = providers.get_provider(provider_name)
    from husk_studio_shared.model_metadata import max_output

    try:
        # Provider call is blocking httpx; keep the event loop free.
        raw = await asyncio.to_thread(
            prov.complete,
            system=DEBUGGER_SYSTEM_PROMPT,
            user=user_content,
            model=model_name,
            api_key=api_key,
            max_output_tokens=max_output(model_name),
        )
    except providers.ProviderError as e:
        # ProviderError messages never contain the key (see providers._post).
        raise DebuggerError(str(e)) from e

    report: DebugReport = extract_report(raw)  # raises ReportParseError on bad JSON
    return await _persist(run_id, report, provider_name, model_name, trigger)


async def _persist(
    run_id: str,
    report: DebugReport,
    provider_name: str,
    model_name: str,
    trigger: str,
) -> dict[str, Any]:
    async with async_session() as s:
        row = DebugReportRow(
            id=_new_id(),
            run_id=run_id,
            failure_node=report.failure_localization.node_id,
            failure_class=report.failure_class,
            report_json=report.model_dump(),
            provider=provider_name,
            model=model_name,
            system_prompt_version=SYSTEM_PROMPT_VERSION,
            confidence=report.confidence,
            trigger=trigger,
            applied=False,
            created_at=int(time.time() * 1000),
        )
        s.add(row)
        await s.commit()
        return serialize_report_row(row)


async def latest_report(run_id: str) -> dict[str, Any] | None:
    async with async_session() as s:
        row = (
            (
                await s.execute(
                    select(DebugReportRow)
                    .where(DebugReportRow.run_id == run_id)
                    .order_by(DebugReportRow.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return serialize_report_row(row) if row else None
