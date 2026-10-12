"""Research Synthesizer -- 5-node pipeline on Husk's own engine.

Resume substrate: this graph runs on Husk's own engine (``husk_studio_shared.engine``):
a linear executor plus a local SQLite snapshot store. After each node a snapshot
of the merged state is persisted; ``replay_from`` resumes a thread from the
snapshot taken before the fork node and re-runs only that node and its
successors, so the upstream nodes emit no spans and consume no tokens.

LLM nodes call the provider for real when a key is set, otherwise emit canned
spans. The benchmark's failure injection (N1-N4) overrides the output of the
LLM call to produce a controlled failure, but the LLM call still happens
so the token/cost numbers in the spans are real.

Layout (state moves left -> right):

    START -> query_expansion -> retrieve -> analyze -> synthesize -> cite_check -> END

`analyze` (large model, large output) is the cost-dominant upstream reasoning
step; `synthesize` is now a cheap downstream formatter. This mirrors real agents
(expensive retrieval/analysis early, cheap formatting late) so a replay that
forks downstream of `analyze` bypasses the bulk of the token cost.

Failure modes (injected by inject_failures.py for the failing 20%):

    None  -> clean success
    "N1"  -> malformed sub-queries
    "N2"  -> retrieve returns empty
    "N3"  -> hallucinated citations
    "N4"  -> cite_check accepts mismatched citations
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# When Husk's replay engine imports this file via importlib (during a
# /api/replay), the `benchmark` package root may not be on sys.path. Insert the
# workspace root so `benchmark.research_agent.*` resolves either way.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from benchmark.research_agent import prompts  # noqa: E402
from benchmark.research_agent.mock_retrieve import (  # noqa: E402
    Source,
    render_sources,
)
from benchmark.research_agent.mock_retrieve import (  # noqa: E402
    retrieve as mock_retrieve,
)
from husk_studio_shared.engine import LinearExecutor, LinearGraph, SnapshotStore  # noqa: E402
from husk_studio_shared.pricing import cost_usd  # noqa: E402
from husk_studio_shared.state_diff import diff_states  # noqa: E402

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OTel + Groq setup
# ---------------------------------------------------------------------------

_otlp_base = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:7654"
).rstrip("/")
ENDPOINT = f"{_otlp_base}/v1/traces"
GRAPH_FILE = str(Path(__file__).resolve())

_FAST = os.environ.get("BENCH_FAST", "0") == "1"
_LATENCY_N1 = 0.001 if _FAST else 4.5        # query_expansion
_LATENCY_N2 = 0.001 if _FAST else 3.0        # retrieve
_LATENCY_ANALYZE = 0.001 if _FAST else 9.5   # analyze (cost-dominant, was synthesize)
_LATENCY_N3 = 0.001 if _FAST else 2.0        # synthesize (now a cheap formatter)
_LATENCY_N4 = 0.001 if _FAST else 5.0        # cite_check

# ---------------------------------------------------------------------------
# HTTP cassettes (model-free replay). When HUSK_RECORD_CASSETTE=1 the LLM HTTP
# calls of a run are recorded under ~/.husk/cassettes/<thread_id>/. When
# HUSK_REPLAY_CASSETTE=1 a replay is served from the parent's cassette instead of
# calling the provider — free, deterministic, byte-identical (cache miss on a
# changed request falls through to the real provider and is recorded). The httpx
# monkeypatch is process-global, so a lock serialises cassette sessions; recording
# is therefore meant for the sequential replay path or a single-worker run.
# ---------------------------------------------------------------------------

_cassette_lock = threading.Lock()


def _cassette_dir(thread_id: str) -> str:
    home = Path(os.environ.get("HUSK_HOME", str(Path.home() / ".husk")))
    return str(home / "cassettes" / thread_id)


@contextmanager
def _cassette_session(thread_id: str, *, record: bool, replay: bool):
    if not (record or replay):
        yield
        return
    from husk_studio_sandbox import cassette  # lazy: keep the graph importable without the sandbox

    with _cassette_lock:
        cm = (
            cassette.replaying(_cassette_dir(thread_id))
            if replay
            else cassette.recording(_cassette_dir(thread_id))
        )
        with cm:
            yield


def _want_record() -> bool:
    return os.environ.get("HUSK_RECORD_CASSETTE") == "1"


def _want_replay_cassette() -> bool:
    return os.environ.get("HUSK_REPLAY_CASSETTE") == "1"


# ---------------------------------------------------------------------------
# LLM provider: Cerebras (preferred when CEREBRAS_API_KEY is set) or Groq.
# Both expose the same OpenAI-style `chat.completions.create` interface, so the
# call path is identical -- only the client and the default model names differ.
# ---------------------------------------------------------------------------

if os.environ.get("OPENROUTER_API_KEY"):
    _PROVIDER = "openrouter"
elif os.environ.get("CEREBRAS_API_KEY"):
    _PROVIDER = "cerebras"
else:
    _PROVIDER = "groq"


def _llm_key() -> str | None:
    # Read at call-time so a backend that boots without the key, then is asked
    # to replay with the key in env, still works. Priority matches _PROVIDER.
    return (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("CEREBRAS_API_KEY")
        or os.environ.get("GROQ_API_KEY")
    )


def _use_llm() -> bool:
    return bool(_llm_key())


# Provider-aware default model names (override with LLM_MODEL_SMALL/LARGE, or the
# legacy GROQ_MODEL_SMALL/LARGE).
if _PROVIDER == "openrouter":
    # Same Llama family as the Groq run → directly comparable; non-reasoning →
    # predictable token counts (no cost blow-up). Cheap on OpenRouter.
    _SMALL_DEFAULT = "meta-llama/llama-3.1-8b-instruct"
    _LARGE_DEFAULT = "meta-llama/llama-3.3-70b-instruct"
elif _PROVIDER == "cerebras":
    _SMALL_DEFAULT, _LARGE_DEFAULT = "llama3.1-8b", "llama-3.3-70b"
else:
    _SMALL_DEFAULT, _LARGE_DEFAULT = "llama-3.1-8b-instant", "llama-3.3-70b-versatile"

MODEL_SMALL = os.environ.get(
    "LLM_MODEL_SMALL", os.environ.get("GROQ_MODEL_SMALL", _SMALL_DEFAULT)
)
MODEL_LARGE = os.environ.get(
    "LLM_MODEL_LARGE", os.environ.get("GROQ_MODEL_LARGE", _LARGE_DEFAULT)
)

MODEL_QUERY_EXPANSION = MODEL_SMALL
# analyze is the cost-dominant upstream node (large model, large output).
MODEL_ANALYZE = MODEL_LARGE
# synthesize is now a cheap downstream formatter (small model, short output).
MODEL_SYNTHESIZE = MODEL_SMALL
MODEL_CITE_CHECK = MODEL_SMALL

_tracer = None
_llm_client = None
_llm_client_key: str | None = None


def _get_tracer():
    global _tracer
    if _tracer is not None:
        return _tracer
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        provider = TracerProvider(
            resource=Resource.create({"service.name": "research-synthesizer"})
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT))
        )
        trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("husk.benchmark.research_agent")
    return _tracer


def _get_client():
    global _llm_client, _llm_client_key
    key = _llm_key()
    if _llm_client is None or _llm_client_key != key:
        # LLM_MAX_RETRIES default 0: on a FREE tier a 429 fails FAST instead of
        # letting the SDK block for minutes on the server's Retry-After (which
        # made a throttled batch take hours). On a PAID tier set LLM_MAX_RETRIES=2
        # so rare transient 429s recover cleanly. The short timeout still caps
        # any single hung call; persistent throttling yields a 0-token span we
        # filter out in analysis.
        retries = int(os.environ.get("LLM_MAX_RETRIES", "0"))
        if _PROVIDER == "openrouter":
            from openai import OpenAI

            _llm_client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=key,
                max_retries=retries,
                timeout=30.0,
            )
        elif _PROVIDER == "cerebras":
            from cerebras.cloud.sdk import Cerebras

            _llm_client = Cerebras(api_key=key, max_retries=retries, timeout=30.0)
        else:
            from groq import Groq

            _llm_client = Groq(api_key=key, max_retries=retries, timeout=30.0)
        _llm_client_key = key
    return _llm_client


def _call_groq(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 400,
    temperature: float = 0.3,
) -> tuple[str, int, int]:
    """Make a real LLM call (Cerebras or Groq). Returns (content, in, out).

    Retries once on transient errors. If no provider key is set, falls back to
    canned numbers so the benchmark still completes. (Name kept for the node
    call sites; the provider is selected by `_PROVIDER`.)
    """
    if not _use_llm():
        # Canned fallback — same numbers as the original synthetic benchmark.
        return (
            "(canned response — set CEREBRAS_API_KEY or GROQ_API_KEY for real LLM calls)",
            len(system) // 4 + len(user) // 4,
            80,
        )

    client = _get_client()
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            choice = resp.choices[0].message.content or ""
            usage = resp.usage
            ti = usage.prompt_tokens if usage else 0
            to = usage.completion_tokens if usage else 0
            return choice, int(ti), int(to)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt == 0:
                time.sleep(0.5)
                continue
    log.warning("%s call failed after retry: %s", _PROVIDER, last_err)
    return f"({_PROVIDER} error: {type(last_err).__name__})", 0, 0


def _set_llm_attrs(
    span, system_name: str, model: str, tokens_in: int, tokens_out: int
) -> None:
    # Record the real provider (cerebras/groq), ignoring the legacy literal
    # passed by the node call sites.
    span.set_attribute("gen_ai.system", _PROVIDER)
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("gen_ai.response.model", model)
    span.set_attribute("gen_ai.usage.input_tokens", tokens_in)
    span.set_attribute("gen_ai.usage.output_tokens", tokens_out)
    c = cost_usd(model, tokens_in, tokens_out)
    if c is not None:
        span.set_attribute("gen_ai.usage.cost_usd", c)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State(TypedDict, total=False):
    topic: str
    failure_mode: str | None  # None | "N1" | "N2" | "N3" | "N4"
    sub_queries: list[str]
    sources: list[dict]
    analysis: str
    answer: str
    verdict: str
    status: str
    error_node: str | None


# ---------------------------------------------------------------------------
# Node 1 -- query_expansion
# ---------------------------------------------------------------------------


def query_expansion(state: State) -> dict:
    tracer = _get_tracer()
    topic = state.get("topic", "")
    fail = state.get("failure_mode")
    with tracer.start_as_current_span("query_expansion") as span:
        span.set_attribute("husk.node", "query_expansion")

        user_msg = prompts.QUERY_EXPANSION_USER.format(topic=topic)
        span.add_event("gen_ai.user.message", {"content": user_msg})

        content, ti, to = _call_groq(
            MODEL_QUERY_EXPANSION,
            prompts.QUERY_EXPANSION_SYSTEM,
            user_msg,
            max_tokens=200,
        )
        _set_llm_attrs(span, "groq", MODEL_QUERY_EXPANSION, ti, to)

        if fail == "N1":
            sub_queries = [topic]
            span.set_attribute("benchmark.failure_mode", "N1")
            span.set_status(trace.Status(trace.StatusCode.ERROR, "malformed expansion"))
            span.add_event(
                "gen_ai.choice",
                {"finish_reason": "stop", "message.content": json.dumps(sub_queries)},
            )
            time.sleep(_LATENCY_N1)
            return {
                "sub_queries": sub_queries,
                "status": "error",
                "error_node": "query_expansion",
            }

        # Try to parse Groq's JSON output, otherwise fall back to a deterministic list.
        sub_queries: list[str] = []
        try:
            parsed = json.loads(content) if content.strip().startswith("[") else None
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                sub_queries = parsed[:5]
        except (json.JSONDecodeError, ValueError):
            pass
        if not sub_queries:
            sub_queries = [
                f"{topic} overview",
                f"{topic} key facts",
                f"{topic} recent developments",
                f"{topic} criticism",
            ]

        span.add_event(
            "gen_ai.choice",
            {"finish_reason": "stop", "message.content": json.dumps(sub_queries)},
        )
        time.sleep(_LATENCY_N1)
        return {"sub_queries": sub_queries}


# ---------------------------------------------------------------------------
# Node 2 -- retrieve (mock tool)
# ---------------------------------------------------------------------------


def retrieve(state: State) -> dict:
    tracer = _get_tracer()
    topic = state.get("topic", "")
    sub_queries = state.get("sub_queries") or [topic]
    fail = state.get("failure_mode")
    with tracer.start_as_current_span("retrieve") as span:
        span.set_attribute("gen_ai.tool.name", prompts.RETRIEVE_TOOL_NAME)
        span.set_attribute("gen_ai.tool.type", "function")
        span.set_attribute("husk.node", "retrieve")
        span.set_attribute("retrieve.query_count", len(sub_queries))

        all_sources: list[Source] = []
        force_empty = fail == "N2"
        for q in sub_queries[:2]:
            all_sources.extend(mock_retrieve(topic, q, force_empty=force_empty))

        span.add_event(
            "gen_ai.tool.message",
            {
                "name": prompts.RETRIEVE_TOOL_NAME,
                "arguments": json.dumps({"queries": sub_queries[:2]}),
                "content": render_sources(all_sources),
            },
        )

        if force_empty:
            span.set_attribute("benchmark.failure_mode", "N2")
            span.set_status(trace.Status(trace.StatusCode.ERROR, "empty result"))
            time.sleep(_LATENCY_N2)
            return {
                "sources": [],
                "status": "error",
                "error_node": "retrieve",
            }

        time.sleep(_LATENCY_N2)
        return {
            "sources": [
                {"id": s.id, "title": s.title, "snippet": s.snippet}
                for s in all_sources
            ]
        }


# ---------------------------------------------------------------------------
# Node 3 -- analyze (LLM, large model) -- cost-dominant upstream reasoning
# ---------------------------------------------------------------------------


def analyze(state: State) -> dict:
    tracer = _get_tracer()
    topic = state.get("topic", "")
    sources = state.get("sources") or []

    with tracer.start_as_current_span("analyze") as span:
        span.set_attribute("husk.node", "analyze")

        sources_rendered = "\n".join(
            f"[{s['id']}] {s['title']}: {s['snippet']}" for s in sources
        )
        user_msg = prompts.ANALYZE_USER.format(
            topic=topic, sources=sources_rendered or "(no sources)"
        )
        span.add_event("gen_ai.user.message", {"content": user_msg})

        if not sources:
            # Upstream N2 produced no sources -- nothing to analyse, propagate.
            _set_llm_attrs(span, "groq", MODEL_ANALYZE, 0, 0)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "no sources"))
            analysis = f"(no sources to analyse for '{topic}')"
        else:
            # The expensive call: large model, large output. This is the bulk
            # of the graph's token cost and the work a downstream replay skips.
            content, ti, to = _call_groq(
                MODEL_ANALYZE,
                prompts.ANALYZE_SYSTEM,
                user_msg,
                max_tokens=700,
            )
            _set_llm_attrs(span, "groq", MODEL_ANALYZE, ti, to)
            analysis = content or f"(analysis unavailable for '{topic}')"

        span.add_event(
            "gen_ai.choice",
            {"finish_reason": "stop", "message.content": analysis},
        )
        time.sleep(_LATENCY_ANALYZE)
        return {"analysis": analysis}


# ---------------------------------------------------------------------------
# Node 4 -- synthesize (LLM, small model) -- cheap downstream formatter
# ---------------------------------------------------------------------------


def synthesize(state: State) -> dict:
    tracer = _get_tracer()
    topic = state.get("topic", "")
    sources = state.get("sources") or []
    fail = state.get("failure_mode")

    with tracer.start_as_current_span("synthesize") as span:
        span.set_attribute("husk.node", "synthesize")

        sources_rendered = "\n".join(
            f"[{s['id']}] {s['title']}: {s['snippet']}" for s in sources
        )
        user_msg = prompts.SYNTHESIZE_USER.format(
            topic=topic,
            sources=sources_rendered or "(no sources)",
            analysis=state.get("analysis") or "(no analysis)",
        )
        span.add_event("gen_ai.user.message", {"content": user_msg})

        if not sources:
            # Upstream N2 already errored -- skip the real LLM call, propagate.
            _set_llm_attrs(span, "groq", MODEL_SYNTHESIZE, 0, 0)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "no sources"))
            answer = f"(could not synthesise -- no sources for '{topic}')"
        else:
            # Cheap formatter: small model, short output -- it just turns the
            # analysis into a cited answer.
            content, ti, to = _call_groq(
                MODEL_SYNTHESIZE,
                prompts.SYNTHESIZE_SYSTEM,
                user_msg,
                max_tokens=150,
            )
            _set_llm_attrs(span, "groq", MODEL_SYNTHESIZE, ti, to)

            if fail == "N3":
                # Override the LLM's content with a known-bad answer (mismatched cites).
                span.set_attribute("benchmark.failure_mode", "N3")
                answer = (
                    f"{topic.capitalize()} can be characterised by three things [1], "
                    f"[2], and [9]. Recent developments [3] confirm earlier "
                    f"speculation [9]. See [9] for further reading."
                )
            else:
                answer = content or (
                    f"{topic.capitalize()} can be characterised through several "
                    f"angles [1][2]. The available evidence [3] suggests a stable "
                    f"trend, though [4] introduces concerns."
                )

        span.add_event(
            "gen_ai.choice",
            {"finish_reason": "stop", "message.content": answer},
        )
        time.sleep(_LATENCY_N3)
        update: dict = {"answer": answer}
        if fail == "N3":
            update["status"] = "error"
            update["error_node"] = "synthesize"
        return update


# ---------------------------------------------------------------------------
# Node 4 -- cite_check (LLM, small model)
# ---------------------------------------------------------------------------


def cite_check(state: State) -> dict:
    tracer = _get_tracer()
    answer = state.get("answer", "")
    sources = state.get("sources") or []
    fail = state.get("failure_mode")

    with tracer.start_as_current_span("cite_check") as span:
        span.set_attribute("husk.node", "cite_check")

        sources_rendered = "\n".join(
            f"[{s['id']}] {s['title']}" for s in sources
        )
        user_msg = prompts.CITE_CHECK_USER.format(
            answer=answer, sources=sources_rendered
        )
        span.add_event("gen_ai.user.message", {"content": user_msg})

        # Real call for token/cost.
        content, ti, to = _call_groq(
            MODEL_CITE_CHECK,
            prompts.CITE_CHECK_SYSTEM,
            user_msg,
            max_tokens=20,
        )
        _set_llm_attrs(span, "groq", MODEL_CITE_CHECK, ti, to)

        # Deterministic verdict logic (we don't trust the LLM here -- this
        # node is a citation auditor, we compute correctness ourselves).
        all_cite_ids = {s["id"] for s in sources}
        cited = set()
        for token in answer.split("["):
            head = token.split("]")[0]
            try:
                cited.add(int(head))
            except ValueError:
                pass
        mismatched = cited - all_cite_ids

        upstream_status = state.get("status")
        upstream_error_node = state.get("error_node")

        if fail == "N4":
            verdict = "valid"
            span.set_attribute("benchmark.failure_mode", "N4")
            status_update: dict = {"status": "error", "error_node": "cite_check"}
        elif mismatched:
            verdict = "mismatch"
            status_update = {"status": "error", "error_node": "cite_check"}
            span.set_status(trace.Status(trace.StatusCode.ERROR, "mismatch"))
        elif upstream_status == "error":
            verdict = "valid" if not mismatched else "mismatch"
            status_update = {
                "status": "error",
                "error_node": upstream_error_node,
            }
        else:
            verdict = "valid"
            status_update = {"status": "ok"}

        span.add_event(
            "gen_ai.choice",
            {"finish_reason": "stop", "message.content": verdict},
        )
        # Use the verdict we computed, but also expose what Groq said for transparency.
        if content:
            span.add_event(
                "gen_ai.choice.llm_response",
                {"finish_reason": "stop", "message.content": content[:120]},
            )
        time.sleep(_LATENCY_N4)
        return {"verdict": verdict, **status_update}


# ---------------------------------------------------------------------------
# Build & invoke
# ---------------------------------------------------------------------------


def _snapshot_db_path() -> str:
    home = Path(os.environ.get("HUSK_HOME", str(Path.home() / ".husk")))
    home.mkdir(parents=True, exist_ok=True)
    # Husk's own checkpoint store. Local-first; nothing leaves the machine.
    return str(home / "husk_snapshots.sqlite")


def _build_graph() -> LinearGraph:
    """Husk's own linear graph: add_node order is the execution order.

    Linear chain:
        query_expansion -> retrieve -> analyze -> synthesize -> cite_check
    """
    g = LinearGraph()
    g.add_node("query_expansion", query_expansion)
    g.add_node("retrieve", retrieve)
    g.add_node("analyze", analyze)
    g.add_node("synthesize", synthesize)
    g.add_node("cite_check", cite_check)
    return g


# Per-node state attribute cap: keep traces (and the OTLP payload) bounded even
# when a node's state carries a long analysis string.
_STATE_ATTR_CAP = 8000


def _safe_state_json(obj: object) -> str:
    s = json.dumps(obj, sort_keys=True, default=str)
    if len(s) > _STATE_ATTR_CAP:
        s = s[:_STATE_ATTR_CAP] + f"…[+{len(s) - _STATE_ATTR_CAP} chars]"
    return s


def _make_node_telemetry(tracer):  # type: ignore[no-untyped-def]
    """Engine telemetry hook: emit a `graph_node` span per node carrying the
    before/after state and their diff, so per-node state is first-class in the
    trace (not reconstructed). The node's own LLM/tool span nests under it.
    """

    @contextmanager
    def on_node(node: str, seq: int, before: dict):  # type: ignore[no-untyped-def]
        with tracer.start_as_current_span(f"node:{node}") as span:
            span.set_attribute("husk.span_kind", "graph_node")
            span.set_attribute("husk.node", node)
            span.set_attribute("husk.node_seq", seq)
            span.set_attribute("husk.state_before", _safe_state_json(before))

            def record(after: dict, delta: dict, error: BaseException | None) -> None:
                span.set_attribute("husk.state_after", _safe_state_json(after))
                span.set_attribute(
                    "husk.state_diff", _safe_state_json(diff_states(before, after))
                )
                if error is not None:
                    span.set_attribute(
                        "husk.error.traceback",
                        "".join(
                            traceback.format_exception(
                                type(error), error, error.__traceback__
                            )
                        ),
                    )
                    span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
                elif (after or {}).get("status") == "error":
                    span.set_status(
                        trace.Status(trace.StatusCode.ERROR, f"{node} set status=error")
                    )

            yield record

    return on_node


def _set_topology_attrs(root) -> None:  # type: ignore[no-untyped-def]
    """Record the graph topology on the root span so the studio + debugger see it
    structurally (not by re-importing the module). Linear graph: edges are the
    consecutive node pairs; there are no conditional edges to recover here.
    """
    names = graph.node_names
    root.set_attribute("husk.graph.nodes", json.dumps(names))
    root.set_attribute(
        "husk.graph.edges",
        json.dumps([[a, b] for a, b in zip(names, names[1:], strict=False)]),
    )


graph = _build_graph()

_store: SnapshotStore | None = None
_store_lock = threading.Lock()


def _get_store() -> SnapshotStore:
    """Lazily open the snapshot store (HUSK_HOME may be set after import)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SnapshotStore(_snapshot_db_path())
        return _store


def invoke(state: dict, thread_id: str | None = None) -> dict:
    tracer = _get_tracer()
    tid = thread_id or str(uuid.uuid4())
    executor = LinearExecutor(graph, _get_store(), on_node=_make_node_telemetry(tracer))

    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("service.name", "research-synthesizer")
        root.set_attribute("husk.thread_id", tid)
        root.set_attribute("husk.graph_module", f"{GRAPH_FILE}:graph")
        root.set_attribute("husk.replay.engine", "husk-native")
        root.set_attribute("husk.benchmark.real_llm", _use_llm())
        _set_topology_attrs(root)
        if state.get("failure_mode"):
            root.set_attribute("benchmark.failure_mode", state["failure_mode"])
        with _cassette_session(tid, record=_want_record(), replay=False):
            # Husk's executor runs each node in order inside this span context,
            # so the nodes' own spans nest under agent.run exactly as before, and
            # a snapshot of the merged state is persisted after every node.
            result = executor.run_full(tid, state)
        root.set_attribute("benchmark.status", result.get("status", "ok"))
        if result.get("status") == "error":
            root.set_status(
                trace.Status(
                    trace.StatusCode.ERROR,
                    f"failed at {result.get('error_node')}",
                )
            )

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()  # type: ignore[attr-defined]

    return {"thread_id": tid, "state": dict(result)}


# ---------------------------------------------------------------------------
# Checkpoint resume (modify-and-replay) -- the real primitive, Husk's own
# ---------------------------------------------------------------------------
#
# `invoke` above is a full run from scratch that snapshots after every node.
# `replay_from` resumes an EXISTING thread from Husk's snapshot store, applies a
# state patch, and re-executes ONLY the fork node + its successors. The upstream
# nodes are not re-run, so they emit no spans and consume no tokens -- that bypass
# is the whole point of Husk.
#
# Mechanic (Husk's own engine, in husk_studio_shared.engine): to RE-RUN node X we load
# the snapshot taken after X's PREDECESSOR (the state that was about to enter X in
# the parent run), apply the patch, and run from X onward. For the first node the
# predecessor is START, so there is no upstream snapshot and the patch seeds a
# full re-run -- the deliberate negative control.


def replay_from(
    *, state_override: dict, parent_thread_id: str, fork_node: str
) -> dict:
    """Resume `parent_thread_id` from Husk's snapshot store and re-run `fork_node`
    onward.

    Applies `state_override` (plus a reset of the stale failure channels) at the
    fork point so the re-run is clean, then resumes via Husk's executor. Returns
    the parent's thread_id (the lineage key the studio backend reads), a unique
    `child_id` used to locate the resulting run, and the final state.
    """
    tracer = _get_tracer()
    executor = LinearExecutor(graph, _get_store(), on_node=_make_node_telemetry(tracer))

    # Clear the parent's stale failure channels. Without this, cite_check's
    # `elif upstream_status == "error"` branch would re-flag the fixed child as
    # an error. (None reads as falsy in the nodes' `.get(...)` checks.)
    patch = {
        **state_override,
        "failure_mode": None,
        "status": None,
        "error_node": None,
    }
    child_id = str(uuid.uuid4())
    # Child snapshots live under a distinct thread so the parent's stay intact
    # (and can be re-forked); the run is still located via husk.replay.child_id.
    child_thread_id = f"{parent_thread_id}::replay::{child_id}"

    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("service.name", "research-synthesizer")
        root.set_attribute("husk.thread_id", parent_thread_id)
        root.set_attribute("husk.graph_module", f"{GRAPH_FILE}:graph")
        root.set_attribute("husk.replay.engine", "husk-native")
        root.set_attribute("husk.benchmark.real_llm", _use_llm())
        # Unique marker so the caller can find THIS child run (parent and child
        # share a thread_id, so thread_id alone is ambiguous).
        root.set_attribute("husk.replay.child_id", child_id)
        root.set_attribute("husk.replay.fork_node", fork_node)
        root.set_attribute("husk.replay.cassette", _want_replay_cassette())
        _set_topology_attrs(root)

        with _cassette_session(parent_thread_id, record=False, replay=_want_replay_cassette()):
            result = executor.resume(
                parent_thread_id=parent_thread_id,
                child_thread_id=child_thread_id,
                fork_node=fork_node,
                patch=patch,
            )

        root.set_attribute("benchmark.status", result.get("status", "ok"))
        if result.get("status") == "error":
            root.set_status(
                trace.Status(
                    trace.StatusCode.ERROR,
                    f"failed at {result.get('error_node')}",
                )
            )

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()  # type: ignore[attr-defined]

    return {
        "thread_id": parent_thread_id,
        "child_id": child_id,
        "state": dict(result),
    }
