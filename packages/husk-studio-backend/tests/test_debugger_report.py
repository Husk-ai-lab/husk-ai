"""Debugger output format + end-to-end analysis with a mocked provider.

Covers the required "debugger output JSON format" surface: strict schema parsing
(including fenced / prose-wrapped output), rejection of malformed output, and a
full analyze→persist round trip without touching the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from husk_studio_backend.debugger.schemas import (
    DebugReport,
    ReportParseError,
    extract_report,
)

_VALID = {
    "failure_localization": {"node_id": "analyze", "step_index": 1, "also_implicated": []},
    "failure_class": "missing handling for empty result",
    "root_cause": "retrieve returned no sources and analyze did not guard for it",
    "evidence": ["node analyze: sources == []"],
    "proposed_fix": {"summary": "guard empty sources", "diff": None, "rationale": "addresses cause"},
    "confidence": "high",
    "missing_information": [],
}


def test_extract_plain_json() -> None:
    rep = extract_report(json.dumps(_VALID))
    assert isinstance(rep, DebugReport)
    assert rep.failure_localization.node_id == "analyze"
    assert rep.confidence == "high"


def test_extract_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(_VALID) + "\n```"
    assert extract_report(fenced).failure_class == _VALID["failure_class"]


def test_extract_prose_wrapped_json() -> None:
    wrapped = "Here is my analysis:\n" + json.dumps(_VALID) + "\nLet me know if you need more."
    assert extract_report(wrapped).root_cause == _VALID["root_cause"]


def test_malformed_raises_parse_error() -> None:
    with pytest.raises(ReportParseError):
        extract_report("not json at all, no object here")


def test_extra_keys_rejected() -> None:
    bad = dict(_VALID, hallucinated_extra="nope")
    with pytest.raises(ReportParseError):
        extract_report(json.dumps(bad))


def test_bad_confidence_rejected() -> None:
    bad = dict(_VALID, confidence="very-high")
    with pytest.raises(ReportParseError):
        extract_report(json.dumps(bad))


# --- end-to-end analyze with a mocked provider ---------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    import husk_studio_backend.db.engine as eng

    eng._async_engine = None
    eng._async_factory = None
    eng._sync_engine = None
    eng._sync_factory = None
    return tmp_path


async def _seed_failed_run() -> None:
    from husk_studio_backend.db.engine import async_session
    from husk_studio_backend.db.models import RunRow, SpanRow

    async with async_session() as s:
        s.add(RunRow(id="r1", script_path="agent.py", started_at=1, finished_at=50, status="error",
                     error_message="failed at analyze"))
        s.add(SpanRow(id="root", run_id="r1", kind="chain", name="agent.run", started_at=1,
                      finished_at=50, status="error",
                      attrs={"husk.graph.nodes": json.dumps(["retrieve", "analyze"])}))
        s.add(SpanRow(id="gn_an", run_id="r1", kind="graph_node", name="node:analyze",
                      started_at=10, finished_at=20, status="error",
                      attrs={"husk.node": "analyze", "husk.node_seq": 1,
                             "husk.state_after": json.dumps({"status": "error"}),
                             "husk.state_diff": json.dumps({"added": {"status": "error"}, "removed": {}, "changed": {}})}))
        await s.commit()


class _FakeProvider:
    name = "anthropic"

    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, **_kwargs: object) -> str:
        return self._text

    def list_models(self) -> list[dict[str, object]]:
        return []


@pytest.mark.asyncio
async def test_analyze_persists_report(fresh_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from husk_studio_backend.db.engine import init_db
    from husk_studio_backend.debugger import report as report_mod

    await init_db()
    await _seed_failed_run()

    monkeypatch.setattr(report_mod.secrets, "resolve_api_key", lambda _p: "test-key")
    monkeypatch.setattr(report_mod.providers, "get_provider", lambda _n: _FakeProvider(json.dumps(_VALID)))

    out = await report_mod.run_debug_analysis("r1", trigger="manual")
    assert out["failure_node"] == "analyze"
    assert out["confidence"] == "high"
    assert out["report"]["failure_localization"]["node_id"] == "analyze"
    assert out["trigger"] == "manual"

    # It is persisted and retrievable.
    latest = await report_mod.latest_report("r1")
    assert latest is not None and latest["id"] == out["id"]


@pytest.mark.asyncio
async def test_analyze_rejects_malformed_model_output(
    fresh_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from husk_studio_backend.db.engine import init_db
    from husk_studio_backend.debugger import report as report_mod

    await init_db()
    await _seed_failed_run()

    monkeypatch.setattr(report_mod.secrets, "resolve_api_key", lambda _p: "test-key")
    monkeypatch.setattr(report_mod.providers, "get_provider", lambda _n: _FakeProvider("sorry, I cannot"))

    with pytest.raises(ReportParseError):
        await report_mod.run_debug_analysis("r1", trigger="manual")


@pytest.mark.asyncio
async def test_debug_reports_table_created_version_unchanged(fresh_db: Path) -> None:
    from husk_studio_backend.config import husk_home
    from husk_studio_backend.db.engine import init_db, verify_recording_version
    from husk_studio_shared import RECORDING_FORMAT_VERSION

    await init_db()
    # The debug_reports table is a pure additive table (no bump of its own); a
    # fresh DB is stamped with the current recording format version.
    assert verify_recording_version(husk_home() / "traces.db") == RECORDING_FORMAT_VERSION
