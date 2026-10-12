"""Dashboard summary — feeds the development home with totals + recent runs.

Single-user and local-first: there is no project scoping and no over-time
production aggregation here. This is a snapshot of the runs you've captured on
this machine while building and debugging your agent — what you have, what it
cost, and the handful you ran most recently to jump back into.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter
from sqlalchemy import case, desc, func, select

from husk_studio_backend.db.engine import async_session
from husk_studio_backend.db.models import RunRow, SpanRow

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
async def summary() -> dict[str, Any]:
    """HTTP route — thin wrapper over the reusable aggregation below."""
    return await compute_dashboard_summary()


async def compute_dashboard_summary() -> dict[str, Any]:
    """Aggregate the development home from the local runs/spans tables.

    Extracted from the `/summary` route so other entry points (the `husk-studio mcp`
    server's `dashboard_summary` tool) can reuse the exact same computation
    instead of duplicating the queries.
    """
    now_ms = int(time.time() * 1000)
    last_24h = now_ms - 24 * 60 * 60_000

    async with async_session() as s:
        # Totals over all captured runs.
        totals_row = (
            await s.execute(
                select(
                    func.count(RunRow.id),
                    func.coalesce(func.sum(RunRow.total_tokens_in), 0),
                    func.coalesce(func.sum(RunRow.total_tokens_out), 0),
                    func.coalesce(func.sum(RunRow.total_cost_usd), 0.0),
                    func.coalesce(
                        func.sum(case((RunRow.status == "error", 1), else_=0)), 0
                    ),
                )
            )
        ).one()
        total_runs, tokens_in, tokens_out, cost_usd, errors = totals_row

        # Last 24h.
        last24 = (
            await s.execute(
                select(
                    func.count(RunRow.id),
                    func.coalesce(func.sum(RunRow.total_tokens_in), 0),
                    func.coalesce(func.sum(RunRow.total_tokens_out), 0),
                    func.coalesce(func.sum(RunRow.total_cost_usd), 0.0),
                ).where(RunRow.started_at >= last_24h)
            )
        ).one()
        runs_24h, tokens_in_24h, tokens_out_24h, cost_24h = last24

        total_spans = (await s.execute(select(func.count(SpanRow.id)))).scalar() or 0

        # Average run latency (finished runs only).
        avg_latency_ms = (
            await s.execute(
                select(func.avg(RunRow.finished_at - RunRow.started_at)).where(
                    RunRow.finished_at.is_not(None)
                )
            )
        ).scalar()

        # By framework.
        by_framework_rows = (
            await s.execute(
                select(RunRow.framework, func.count(RunRow.id))
                .group_by(RunRow.framework)
                .order_by(desc(func.count(RunRow.id)))
            )
        ).all()
        by_framework = [
            {"framework": fw or "unknown", "count": int(c)} for fw, c in by_framework_rows
        ]

        # Recent runs (last 5) — the worklist you jump back into.
        recent_rows = (
            (await s.execute(select(RunRow).order_by(RunRow.started_at.desc()).limit(5)))
            .scalars()
            .all()
        )
        recent = [_run_summary(r) for r in recent_rows]

        # 12 buckets across the last 24h for the sparkline (count of runs per ~2h).
        bucket_ms = (24 * 60 * 60_000) // 12
        sparkline: list[int] = []
        for i in range(12):
            lo = last_24h + i * bucket_ms
            hi = lo + bucket_ms
            c = (
                await s.execute(
                    select(func.count(RunRow.id)).where(
                        RunRow.started_at >= lo, RunRow.started_at < hi
                    )
                )
            ).scalar() or 0
            sparkline.append(int(c))

    return {
        "now_ms": now_ms,
        "totals": {
            "runs": int(total_runs or 0),
            "spans": int(total_spans),
            "tokens_in": int(tokens_in or 0),
            "tokens_out": int(tokens_out or 0),
            "cost_usd": float(cost_usd or 0.0),
            "errors": int(errors or 0),
            "avg_latency_ms": int(avg_latency_ms or 0),
        },
        "last_24h": {
            "runs": int(runs_24h or 0),
            "tokens_in": int(tokens_in_24h or 0),
            "tokens_out": int(tokens_out_24h or 0),
            "cost_usd": float(cost_24h or 0.0),
        },
        "by_framework": by_framework,
        "recent_runs": recent,
        "sparkline": sparkline,
    }


def _run_summary(r: RunRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "framework": r.framework,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "duration_ms": (r.finished_at - r.started_at) if r.finished_at else None,
        "total_tokens_in": r.total_tokens_in,
        "total_tokens_out": r.total_tokens_out,
        "total_cost_usd": r.total_cost_usd,
        "script_path": r.script_path,
    }
