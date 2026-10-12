"""Regressions for the defects found in the performance/bug/feature audit.

Each test here failed before its corresponding fix. They are grouped by the thing
that was broken, not by module, so a future change that reintroduces one of these
fails with an obvious name.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("HUSK_HOME", tempfile.mkdtemp())
os.environ["HUSK_NO_AUTO_BUILD"] = "1"

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from husk_studio_backend.db.engine import init_db  # noqa: E402
from husk_studio_backend.ingest.otel_parser import parse_otlp_traces  # noqa: E402
from husk_studio_backend.main import app  # noqa: E402

# ── helpers ──────────────────────────────────────────────────────────────────


def _span(
    sid: str,
    *,
    parent: str | None = None,
    start: int = 1_000_000,
    end: int | None = 2_000_000,
    attrs: list[dict] | None = None,
    events: list[dict] | None = None,
    trace: str = "aa" * 16,
) -> dict:
    s: dict = {
        "traceId": trace,
        "spanId": sid,
        "name": "span-" + sid,
        "startTimeUnixNano": str(start),
        "attributes": attrs or [],
    }
    if parent:
        s["parentSpanId"] = parent
    if end is not None:
        s["endTimeUnixNano"] = str(end)
    if events:
        s["events"] = events
    return s


def _body(spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "my-agent"}}
                    ]
                },
                "scopeSpans": [{"spans": spans}],
            }
        ]
    }


def _attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


async def _client() -> AsyncClient:
    await init_db()
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:7654"
    )


# ── secret redaction ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghij0123456789ABCDEFGH",  # OpenAI
        "sk-ant-abcdefghij0123456789ABCD",  # Anthropic
        # Synthetic, never a real key: the point is the hyphens after the `sk-`
        # prefix, which is what the old pattern choked on.
        "sk-or-v1-0000ffff1111eeee2222dddd3333cccc4444bbbb5555aaaa6666",  # OpenRouter
        "sk-proj-abcdefghij0123456789ABCDEFGH",  # OpenAI project key
        "gsk_abcdefghij0123456789ABCDEFGHIJKLMNOPQRSTUVWX",  # Groq
        "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUVWXYZ0123456789abcd",  # GitHub
        "ghp_abcdefghij0123456789ABCDEFGHIJKLMNOP",  # GitHub classic
        "AKIAIOSFODNN7EXAMPLE",  # AWS
    ],
)
def test_redaction_covers_hyphenated_and_underscored_key_formats(secret: str) -> None:
    """The old `sk-[A-Za-z0-9]{20,}` stopped at the first hyphen, so OpenRouter and
    OpenAI project keys survived in the clear — as did Groq and GitHub formats."""
    spans = parse_otlp_traces(
        _body(
            [
                _span(
                    "01" * 8,
                    events=[
                        {
                            "name": "gen_ai.user.message",
                            "attributes": [_attr("content", f"my key is {secret}")],
                        }
                    ],
                )
            ]
        )
    )
    assert secret not in str(spans[0].input_inline)
    assert "REDACTED" in str(spans[0].input_inline)


def test_span_attributes_are_redacted() -> None:
    """`attrs` skipped the redactor entirely, yet it is persisted, served over the
    API, and included in `husk-studio export` — the bundle we tell users to attach to
    bug reports."""
    secret = "sk-abcdefghij0123456789ABCDEFGH"
    spans = parse_otlp_traces(
        _body([_span("01" * 8, attrs=[_attr("my.config", secret)])])
    )
    assert secret not in str(spans[0].attrs)


def test_husk_namespace_attributes_are_left_verbatim() -> None:
    """Husk reads these back operationally — replay imports `husk.graph_module` —
    so redacting them would break replay for an unlucky path."""
    path = "/home/u/token=abcdefgh/agent.py:graph"
    spans = parse_otlp_traces(
        _body([_span("01" * 8, attrs=[_attr("husk.graph_module", path)])])
    )
    assert spans[0].attrs["husk.graph_module"] == path


# ── run lifecycle ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_stays_running_until_its_root_span_finishes() -> None:
    """A finished *child* used to complete the whole run, so a live agent showed up
    as `success` — which also made the Studio skip the live WebSocket."""
    trace = "b1" * 16
    async with await _client() as c:
        await c.post(
            "/v1/traces",
            json=_body(
                [
                    _span("b1" * 8, end=None, trace=trace),  # root still open
                    _span("b2" * 8, parent="b1" * 8, trace=trace),  # child done
                ]
            ),
        )
        run = (await c.get(f"/api/v1/runs/{trace[:26]}")).json()
        assert run["status"] == "running"

        # Root closes -> now the run is genuinely complete.
        await c.post(
            "/v1/traces", json=_body([_span("b1" * 8, end=9_000_000, trace=trace)])
        )
        assert (await c.get(f"/api/v1/runs/{trace[:26]}")).json()["status"] == "success"


@pytest.mark.asyncio
async def test_token_usage_arriving_on_a_later_export_is_not_lost() -> None:
    """Streaming LLM spans often end before their token counts are known. The update
    branch ignored usage, so those runs under-reported tokens and cost forever."""
    trace = "c2" * 16
    usage = [
        _attr("gen_ai.operation.name", "chat"),
        _attr("gen_ai.request.model", "gpt-4o"),
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "50"}},
    ]
    async with await _client() as c:
        await c.post("/v1/traces", json=_body([_span("c3" * 8, trace=trace)]))
        run = (await c.get(f"/api/v1/runs/{trace[:26]}")).json()
        assert run["total_tokens_in"] == 0

        # Same span id, re-exported with usage attached.
        await c.post(
            "/v1/traces", json=_body([_span("c3" * 8, trace=trace, attrs=usage)])
        )
        run = (await c.get(f"/api/v1/runs/{trace[:26]}")).json()
        assert run["total_tokens_in"] == 100
        assert run["total_tokens_out"] == 50

        # Idempotent: replaying the same export must not double-count.
        await c.post(
            "/v1/traces", json=_body([_span("c3" * 8, trace=trace, attrs=usage)])
        )
        run = (await c.get(f"/api/v1/runs/{trace[:26]}")).json()
        assert run["total_tokens_in"] == 100
        assert run["total_tokens_out"] == 50


# ── delete a single run ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_run_removes_it_but_keeps_replays_forked_from_it() -> None:
    trace = "d3" * 16
    async with await _client() as c:
        await c.post("/v1/traces", json=_body([_span("d4" * 8, trace=trace)]))
        run_id = trace[:26]
        assert (await c.get(f"/api/v1/runs/{run_id}")).status_code == 200
        assert (await c.get(f"/api/v1/runs/{run_id}/spans")).json()

        assert (await c.delete(f"/api/v1/runs/{run_id}")).status_code == 204
        assert (await c.get(f"/api/v1/runs/{run_id}")).status_code == 404
        assert (await c.get(f"/api/v1/runs/{run_id}/spans")).json() == []
        assert (await c.delete(f"/api/v1/runs/{run_id}")).status_code == 404


@pytest.mark.asyncio
async def test_delete_run_refuses_a_cross_origin_caller() -> None:
    """Deleting is destructive, so unlike the read routes it sits behind the
    loopback guard — a page the user is viewing must not be able to drive it."""
    trace = "e4" * 16
    async with await _client() as c:
        await c.post("/v1/traces", json=_body([_span("e5" * 8, trace=trace)]))
        r = await c.delete(
            f"/api/v1/runs/{trace[:26]}", headers={"Origin": "http://evil.example"}
        )
        assert r.status_code == 403
        assert (await c.get(f"/api/v1/runs/{trace[:26]}")).status_code == 200


# ── span list ceiling ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_span_list_limit_is_clamped() -> None:
    """The endpoint was unbounded; each span carries a full prompt and completion."""
    trace = "f5" * 16
    async with await _client() as c:
        await c.post("/v1/traces", json=_body([_span("f6" * 8, trace=trace)]))
        r = await c.get(f"/api/v1/runs/{trace[:26]}/spans", params={"limit": 10**9})
        assert r.status_code == 200
        assert len(r.json()) <= 5000
