"""Structural determinism of Husk's checkpoint-resume replay.

The load-bearing, genuinely-deterministic property behind the "100% replay
success" and token-bypass claims: resuming a parent run at a fork node re-runs
*exactly* that node and its successors, and nothing upstream. This exercises
Husk's own engine (``husk_studio_shared.engine``): it records a real run (canned LLM, no
API key) and asserts the replay's executed node
set AND its child LLM-span count equal {fork} ∪ successors, with zero variance
across repeats. It goes red the instant replay drifts (e.g. an off-by-one in the
predecessor logic).
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

# The research graph reads HUSK_HOME and BENCH_FAST at import time (it picks the
# snapshot-store path and bakes in node sleeps), so set both before importing it:
# a throwaway home keeps ~/.husk untouched, and fast mode collapses the node
# sleeps so the suite runs in milliseconds, not minutes.
os.environ["HUSK_HOME"] = tempfile.mkdtemp(prefix="husk-determinism-")
os.environ["BENCH_FAST"] = "1"

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from benchmark.research_agent import graph as G  # noqa: E402

NODE_NAMES = {"query_expansion", "retrieve", "analyze", "synthesize", "cite_check"}

# Resuming at `fork` must re-run exactly these nodes (fork + everything after).
EXPECTED: dict[str, set[str]] = {
    "cite_check": {"cite_check"},
    "synthesize": {"synthesize", "cite_check"},
    "analyze": {"analyze", "synthesize", "cite_check"},
    "retrieve": {"retrieve", "analyze", "synthesize", "cite_check"},
    "query_expansion": set(NODE_NAMES),  # predecessor is START -> full re-run
}

# Child LLM-span count per fork. `retrieve` is a TOOL span (no model call), so it
# is not counted -- which is why the failure-mode counts are N1=4, N2=3, N3=2,
# N4=1:
#   N1 fork=query_expansion -> query_expansion, analyze, synthesize, cite_check = 4
#   N2 fork=retrieve        -> analyze, synthesize, cite_check                  = 3
#   N3 fork=synthesize      -> synthesize, cite_check                           = 2
#   N4 fork=cite_check      -> cite_check                                       = 1
EXPECTED_LLM: dict[str, int] = {
    "query_expansion": 4,
    "retrieve": 3,
    "analyze": 3,
    "synthesize": 2,
    "cite_check": 1,
}

# Nodes strictly upstream of a fork (must stay untouched by the replay).
UPSTREAM_OF: dict[str, set[str]] = {
    "cite_check": {"query_expansion", "retrieve", "analyze", "synthesize"},
}


def _is_llm_span(span) -> bool:  # noqa: ANN001 - ReadableSpan, kept untyped for the test
    """Classify a span as an LLM span the same way the OTel ingest does."""
    attrs = dict(span.attributes or {})
    return "gen_ai.operation.name" in attrs and "gen_ai.tool.name" not in attrs


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    G._tracer = None  # force the graph to bind to our in-memory provider
    return exp


def _replay_spans(exp, parent_thread_id, fork, state_override=None):  # noqa: ANN001
    exp.clear()
    G.replay_from(
        state_override=state_override or {},
        parent_thread_id=parent_thread_id,
        fork_node=fork,
    )
    return exp.get_finished_spans()


@pytest.mark.parametrize("fork", sorted(EXPECTED))
def test_replay_reruns_exactly_fork_and_successors(
    exporter: InMemorySpanExporter, fork: str
) -> None:
    tid = str(uuid.uuid4())
    G.invoke({"topic": f"determinism-{fork}", "failure_mode": None}, thread_id=tid)
    spans = _replay_spans(exporter, tid, fork)
    node_set = {s.name for s in spans if s.name in NODE_NAMES}
    assert node_set == EXPECTED[fork]


@pytest.mark.parametrize("fork", sorted(EXPECTED_LLM))
def test_replay_child_llm_span_count(exporter: InMemorySpanExporter, fork: str) -> None:
    """Structural: the child's LLM-span count is exactly {fork} ∪ successors."""
    tid = str(uuid.uuid4())
    G.invoke({"topic": f"llm-count-{fork}", "failure_mode": None}, thread_id=tid)
    spans = _replay_spans(exporter, tid, fork)
    llm = [s for s in spans if _is_llm_span(s)]
    assert len(llm) == EXPECTED_LLM[fork]


def test_replay_has_zero_variance(exporter: InMemorySpanExporter) -> None:
    observed_nodes: set[frozenset[str]] = set()
    observed_counts: set[int] = set()
    for i in range(3):
        tid = str(uuid.uuid4())
        G.invoke({"topic": f"determinism-rep-{i}", "failure_mode": None}, thread_id=tid)
        spans = _replay_spans(exporter, tid, "synthesize")
        observed_nodes.add(frozenset(s.name for s in spans if s.name in NODE_NAMES))
        observed_counts.add(sum(1 for s in spans if _is_llm_span(s)))
    assert observed_nodes == {frozenset(EXPECTED["synthesize"])}, observed_nodes
    assert observed_counts == {EXPECTED_LLM["synthesize"]}, observed_counts


def test_no_tokens_or_spans_on_skip(
    exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A late-failure replay makes ZERO model calls and emits ZERO spans upstream.

    This is the property the token-bypass metric is built on: a skipped upstream
    node produces no span and no tokens, not a cheaper one.
    """
    tid = str(uuid.uuid4())
    # Parent fails at the LAST node (N4 / cite_check); everything upstream of the
    # fork is already computed and must NOT be touched by the replay.
    G.invoke({"topic": "skip-test", "failure_mode": "N4"}, thread_id=tid)

    calls: list[str] = []
    real_call = G._call_groq

    def _spy(model, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        calls.append(model)
        return real_call(model, *args, **kwargs)

    monkeypatch.setattr(G, "_call_groq", _spy)

    spans = _replay_spans(exporter, tid, "cite_check")
    upstream = {s.name for s in spans if s.name in UPSTREAM_OF["cite_check"]}
    assert upstream == set(), f"upstream nodes emitted spans on a skip replay: {upstream}"
    assert len(calls) == 1, f"expected exactly one model call (cite_check), got {calls}"
    assert sum(1 for s in spans if _is_llm_span(s)) == 1
