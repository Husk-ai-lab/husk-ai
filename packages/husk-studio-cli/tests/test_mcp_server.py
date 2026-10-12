"""Tests for the `husk-studio mcp` server.

Each test runs against an isolated, seeded SQLite DB under a temp HUSK_HOME. We
reset the engine module's cached engines so init_db() rebinds to the temp path
inside the test's own event loop (pytest-asyncio auto mode → one loop per test).
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from husk_studio.cli import main
from husk_studio_backend.db import engine as dbengine


def _reset_engine_cache() -> None:
    dbengine._async_engine = None
    dbengine._async_factory = None
    dbengine._sync_engine = None
    dbengine._sync_factory = None


async def _seed() -> None:
    from husk_studio_backend.db.engine import async_session, init_db
    from husk_studio_backend.db.models import CursorEventRow, RunRow, SpanRow

    await init_db()
    async with async_session() as s:
        # A successful agent run with a root chain span + one LLM child.
        s.add(
            RunRow(
                id="R_ok",
                script_path="agent.py",
                framework="otel/openai",
                status="success",
                started_at=2000,
                finished_at=2500,
                total_tokens_in=42,
                total_tokens_out=18,
                total_cost_usd=0.001,
                extra_metadata={"husk.graph_module": "agent.py:graph"},
            )
        )
        s.add(
            SpanRow(
                id="S_root",
                run_id="R_ok",
                parent_span_id=None,
                kind="chain",
                name="agent.run",
                started_at=2000,
                finished_at=2500,
                status="success",
                attrs={},
            )
        )
        s.add(
            SpanRow(
                id="S_llm",
                run_id="R_ok",
                parent_span_id="S_root",
                kind="llm",
                name="chat gpt-4o-mini",
                started_at=2100,
                finished_at=2300,
                status="success",
                provider="openai",
                model="gpt-4o-mini",
                tokens_in=42,
                tokens_out=18,
                cost_usd=0.001,
                input_inline={"messages": [{"role": "user", "content": "hi"}]},
                output_inline={"content": "hello there"},
                attrs={},
            )
        )
        # A newer, failed run.
        s.add(
            RunRow(
                id="R_err",
                script_path="agent.py",
                framework="unknown",
                status="error",
                started_at=3000,
                finished_at=3100,
                error_message="boom",
            )
        )
        s.add(
            SpanRow(
                id="S_err",
                run_id="R_err",
                parent_span_id=None,
                kind="llm",
                name="chat gpt-4o",
                started_at=3000,
                finished_at=3100,
                status="error",
                provider="openai",
                model="gpt-4o",
                tokens_in=10,
                tokens_out=0,
                error_payload={"type": "ValueError", "message": "boom"},
                attrs={},
            )
        )
        s.add(
            CursorEventRow(
                id="E1",
                hook="afterFileEdit",
                project="demo",
                payload={"file_path": "a.py"},
                created_at=1234,
            )
        )
        await s.commit()


@pytest.fixture
async def server(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    monkeypatch.delenv("HUSK_MCP_ENABLE_REPLAY", raising=False)
    _reset_engine_cache()
    await _seed()

    from husk_studio.mcp.server import build_server

    srv = build_server(enable_replay=False)
    try:
        yield srv
    finally:
        if dbengine._async_engine is not None:
            await dbengine._async_engine.dispose()
        _reset_engine_cache()


async def _call(server: Any, name: str, **args: Any) -> Any:
    """Invoke a tool via the real MCP path and return its payload.

    List-returning tools are wrapped by the SDK as {"result": [...]}; unwrap that.
    """
    _content, structured = await server.call_tool(name, args)
    if isinstance(structured, dict) and list(structured.keys()) == ["result"]:
        return structured["result"]
    return structured


async def test_list_runs_newest_first(server: Any) -> None:
    runs = await _call(server, "list_runs")
    assert [r["id"] for r in runs] == ["R_err", "R_ok"]
    ok = next(r for r in runs if r["id"] == "R_ok")
    assert ok["framework"] == "otel/openai"  # free-form string survives
    assert ok["duration_ms"] == 500
    assert ok["metadata"] == {"husk.graph_module": "agent.py:graph"}


async def test_list_runs_filter_status(server: Any) -> None:
    runs = await _call(server, "list_runs", status="error")
    assert [r["id"] for r in runs] == ["R_err"]


async def test_get_run(server: Any) -> None:
    run = await _call(server, "get_run", run_id="R_ok")
    assert run["id"] == "R_ok"
    assert run["total_cost_usd"] == 0.001
    missing = await _call(server, "get_run", run_id="nope")
    assert "error" in missing


async def test_get_trace_builds_hierarchy(server: Any) -> None:
    trace = await _call(server, "get_trace", run_id="R_ok")
    assert trace["span_count"] == 2
    assert len(trace["spans"]) == 1  # single root
    root = trace["spans"][0]
    assert root["id"] == "S_root"
    assert [c["id"] for c in root["children"]] == ["S_llm"]


async def test_get_trace_truncates_io(server: Any) -> None:
    trace = await _call(server, "get_trace", run_id="R_ok", max_io_chars=5)
    llm = trace["spans"][0]["children"][0]
    assert isinstance(llm["output_inline"], str) and "truncated" in llm["output_inline"]
    # include_io=False drops the fields entirely
    trace2 = await _call(server, "get_trace", run_id="R_ok", include_io=False)
    llm2 = trace2["spans"][0]["children"][0]
    assert "input_inline" not in llm2 and "output_inline" not in llm2


async def test_get_span_full_io(server: Any) -> None:
    span = await _call(server, "get_span", run_id="R_ok", span_id="S_llm")
    assert span["output_inline"] == {"content": "hello there"}  # not truncated
    wrong = await _call(server, "get_span", run_id="R_err", span_id="S_llm")
    assert "error" in wrong  # span belongs to a different run


async def test_list_errors(server: Any) -> None:
    errs = await _call(server, "list_errors")
    assert [r["id"] for r in errs["failed_runs"]] == ["R_err"]
    assert errs["failed_spans"][0]["error_payload"]["type"] == "ValueError"


async def test_cost_breakdown(server: Any) -> None:
    cb = await _call(server, "cost_breakdown")
    assert cb["totals"]["tokens_in"] == 52  # 42 + 10
    mini = next(r for r in cb["by_model"] if r["model"] == "gpt-4o-mini")
    assert mini["cost_usd"] == 0.001 and mini["estimated"] is False
    # gpt-4o span had no stored cost but a known model → estimated from pricing
    big = next(r for r in cb["by_model"] if r["model"] == "gpt-4o")
    assert big["estimated"] is True

    scoped = await _call(server, "cost_breakdown", run_id="R_ok")
    assert scoped["totals"]["tokens_in"] == 42


async def test_dashboard_summary(server: Any) -> None:
    summary = await _call(server, "dashboard_summary")
    assert summary["totals"]["runs"] == 2
    assert summary["totals"]["errors"] == 1


async def test_list_cursor_events(server: Any) -> None:
    events = await _call(server, "list_cursor_events")
    assert events[0]["hook"] == "afterFileEdit"
    assert await _call(server, "list_cursor_events", project="nope") == []


async def test_replay_gating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUSK_MCP_ENABLE_REPLAY", raising=False)
    from husk_studio.mcp.server import build_server

    ro = build_server(enable_replay=False)
    assert "replay_run" not in {t.name for t in await ro.list_tools()}

    rw = build_server(enable_replay=True)
    assert "replay_run" in {t.name for t in await rw.list_tools()}

    # The env var alone flips the gate on.
    monkeypatch.setenv("HUSK_MCP_ENABLE_REPLAY", "1")
    env_on = build_server(enable_replay=False)
    assert "replay_run" in {t.name for t in await env_on.list_tools()}


def test_doctor_reports_mcp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "mcp:" in result.output
    assert "replay:" in result.output


def test_help_lists_mcp() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "mcp" in result.output
