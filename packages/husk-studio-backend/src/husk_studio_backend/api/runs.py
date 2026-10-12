from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, desc, func, select, update

from husk_studio_backend.api._guard import local_only
from husk_studio_backend.config import runs_dir
from husk_studio_backend.db.engine import async_session
from husk_studio_backend.db.models import RunRow, SpanRow

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


async def _models_for_runs(s: Any, run_ids: list[str]) -> dict[str, list[str]]:
    """Distinct model ids used by each run (from its spans) — so the runs list and
    run header can show which model(s) a run touched without an N+1."""
    if not run_ids:
        return {}
    rows = (
        await s.execute(
            select(SpanRow.run_id, SpanRow.model)
            .where(SpanRow.run_id.in_(run_ids), SpanRow.model.is_not(None))
            .distinct()
        )
    ).all()
    out: dict[str, list[str]] = {}
    for rid, m in rows:
        out.setdefault(rid, [])
        if m and m not in out[rid]:
            out[rid].append(m)
    for rid in out:
        out[rid].sort()
    return out


@router.get("")
async def list_runs(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    framework: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """Recent runs, newest first. Optional filters: ``status``, ``framework``
    (substring), and ``q`` (substring match on script_path or run id)."""
    async with async_session() as s:
        query = select(RunRow).order_by(RunRow.started_at.desc())
        if status:
            query = query.where(RunRow.status == status)
        if framework:
            query = query.where(RunRow.framework.like(f"%{framework}%"))
        if q:
            like = f"%{q}%"
            query = query.where(RunRow.script_path.like(like) | RunRow.id.like(like))
        rows = (await s.execute(query.limit(limit).offset(offset))).scalars().all()
        models = await _models_for_runs(s, [r.id for r in rows])
        return [_serialize(r, models.get(r.id, [])) for r in rows]


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    async with async_session() as s:
        row = await s.get(RunRow, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        models = (await _models_for_runs(s, [run_id])).get(run_id, [])
        return _serialize(row, models)


@router.delete("/{run_id}", dependencies=[Depends(local_only)], status_code=204)
async def delete_run(run_id: str) -> None:
    """Delete one run and everything attached to it.

    Until this existed the only way to remove a run was `husk-studio clean`, which wipes
    the entire database — too blunt when one noisy run is in the way. Deleting is
    destructive, so unlike the read routes this one sits behind the loopback guard.

    Spans, snapshots, branches, cassettes, and debug reports go with it via
    ``ON DELETE CASCADE`` (``PRAGMA foreign_keys=ON`` is set per connection).
    Replays forked *from* this run are kept and simply lose their parent pointer —
    silently destroying a user's replays because they deleted the original would be
    a nasty surprise.
    """
    async with async_session() as s:
        row = await s.get(RunRow, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        await s.execute(
            update(RunRow)
            .where(RunRow.parent_run_id == run_id)
            .values(parent_run_id=None, fork_span_id=None)
        )
        await s.delete(row)
        await s.commit()

    # Best-effort: the on-disk payloads (inputs/outputs/snapshots/cassettes).
    # The DB row is already gone, so a filesystem error must not fail the request.
    try:
        shutil.rmtree(runs_dir() / run_id, ignore_errors=True)
    except OSError:  # pragma: no cover - defensive
        pass


@router.get("/{run_id}/breakdown")
async def run_breakdown(run_id: str) -> dict[str, Any]:
    """Per-run cost/usage broken down by (model, provider). This is the report's
    "insight gigantesco": in a multi-model run, see exactly which model did what
    and what each cost — and which models erred."""
    async with async_session() as s:
        run = await s.get(RunRow, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        rows = (
            await s.execute(
                select(
                    SpanRow.model,
                    SpanRow.provider,
                    func.count(SpanRow.id),
                    func.coalesce(func.sum(SpanRow.tokens_in), 0),
                    func.coalesce(func.sum(SpanRow.tokens_out), 0),
                    func.coalesce(func.sum(SpanRow.cost_usd), 0.0),
                    func.coalesce(
                        func.sum(case((SpanRow.status == "error", 1), else_=0)), 0
                    ),
                )
                .where(SpanRow.run_id == run_id, SpanRow.model.is_not(None))
                .group_by(SpanRow.model, SpanRow.provider)
                .order_by(desc(func.coalesce(func.sum(SpanRow.cost_usd), 0.0)))
            )
        ).all()
    by_model = [
        {
            "model": m,
            "provider": p,
            "calls": int(calls or 0),
            "tokens_in": int(ti or 0),
            "tokens_out": int(to or 0),
            "cost_usd": round(float(co or 0.0), 6),
            "errors": int(errs or 0),
        }
        for m, p, calls, ti, to, co, errs in rows
    ]
    total_cost = round(sum(b["cost_usd"] for b in by_model), 6)
    for b in by_model:
        b["cost_share"] = round(b["cost_usd"] / total_cost, 4) if total_cost > 0 else 0.0
    return {"run_id": run_id, "total_cost_usd": total_cost, "by_model": by_model}


def _serialize(r: RunRow, models: list[str]) -> dict[str, Any]:
    return {
        "id": r.id,
        "parent_run_id": r.parent_run_id,
        "fork_span_id": r.fork_span_id,
        "script_path": r.script_path,
        "framework": r.framework,
        "status": r.status,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "total_tokens_in": r.total_tokens_in,
        "total_tokens_out": r.total_tokens_out,
        "total_cost_usd": r.total_cost_usd,
        "error_message": r.error_message,
        "models": models,
    }
