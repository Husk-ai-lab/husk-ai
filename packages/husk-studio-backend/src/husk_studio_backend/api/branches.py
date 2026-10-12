"""Branches: the parent -> child links a replay creates.

A branch records that `child_run_id` was produced by resuming `parent_run_id`
at `fork_span_id` with an override. The replay endpoint creates one automatically
once the child run is ingested; the Studio reads them to draw run lineage and to
show how many LLM tokens (and how much cost) the replay bypassed.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from husk_studio_backend.db.engine import async_session
from husk_studio_backend.db.models import BranchRow, RunRow, SpanRow
from husk_studio_shared.pricing import cost_usd

router = APIRouter(prefix="/api/v1/branches", tags=["branches"])
log = logging.getLogger(__name__)


class CreateBranchRequest(BaseModel):
    parent_run_id: str
    child_run_id: str
    fork_span_id: str
    override_type: str = "input_replace"
    override_payload: dict[str, Any] = Field(default_factory=dict)
    label: str | None = None
    notes: str | None = None


def _new_id() -> str:
    # 26 chars to fit the ULID-shaped id column; uuid4 entropy is ample here.
    return uuid.uuid4().hex[:26]


async def _llm_token_cost(session: AsyncSession, run_id: str) -> tuple[int, float]:
    rows = (
        (await session.execute(select(SpanRow).where(SpanRow.run_id == run_id, SpanRow.kind == "llm")))
        .scalars()
        .all()
    )
    tokens = sum((s.tokens_in or 0) + (s.tokens_out or 0) for s in rows)
    cost = sum(cost_usd(s.model, s.tokens_in, s.tokens_out) or 0.0 for s in rows)
    return tokens, round(cost, 6)


async def _serialize(session: AsyncSession, b: BranchRow) -> dict[str, Any]:
    parent_tokens, parent_cost = await _llm_token_cost(session, b.parent_run_id)
    child_tokens, child_cost = await _llm_token_cost(session, b.child_run_id)
    bypassed = max(0, parent_tokens - child_tokens)
    return {
        "id": b.id,
        "parent_run_id": b.parent_run_id,
        "child_run_id": b.child_run_id,
        "fork_span_id": b.fork_span_id,
        "override_type": b.override_type,
        "override_payload": b.override_payload,
        "label": b.label,
        "notes": b.notes,
        "created_at": b.created_at,
        "parent_llm_tokens": parent_tokens,
        "child_llm_tokens": child_tokens,
        "tokens_bypassed": bypassed,
        "token_bypass_pct": round(100.0 * bypassed / parent_tokens, 2) if parent_tokens else 0.0,
        "cost_bypassed_usd": round(max(0.0, parent_cost - child_cost), 6),
    }


async def record_branch(req: CreateBranchRequest) -> dict[str, Any]:
    """Create (idempotently) the branch row for a parent->child replay."""
    async with async_session() as s:
        existing = (
            (await s.execute(select(BranchRow).where(BranchRow.child_run_id == req.child_run_id)))
            .scalars()
            .first()
        )
        if existing is not None:
            return await _serialize(s, existing)
        if await s.get(RunRow, req.parent_run_id) is None or await s.get(RunRow, req.child_run_id) is None:
            raise HTTPException(status_code=404, detail="parent or child run not found")
        row = BranchRow(
            id=_new_id(),
            parent_run_id=req.parent_run_id,
            child_run_id=req.child_run_id,
            fork_span_id=req.fork_span_id,
            override_payload=req.override_payload,
            override_type=req.override_type,
            created_at=int(time.time() * 1000),
            label=req.label,
            notes=req.notes,
        )
        s.add(row)
        await s.commit()
        return await _serialize(s, row)


@router.post("")
async def create_branch(req: CreateBranchRequest) -> dict[str, Any]:
    return await record_branch(req)


@router.get("")
async def list_branches(
    parent_run_id: str | None = None,
    child_run_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List branches, optionally filtered to a parent or child run."""
    async with async_session() as s:
        q = select(BranchRow).order_by(BranchRow.created_at.desc()).limit(limit)
        if parent_run_id:
            q = q.where(BranchRow.parent_run_id == parent_run_id)
        if child_run_id:
            q = q.where(BranchRow.child_run_id == child_run_id)
        rows = (await s.execute(q)).scalars().all()
        return [await _serialize(s, b) for b in rows]
