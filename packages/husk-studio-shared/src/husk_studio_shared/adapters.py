"""One-call framework adapters: auto-instrument an SDK so Husk captures it.

These are thin wiring over the OpenInference instrumentors, which already emit
the GenAI-semconv spans Husk's OTLP ingest understands (chat, tool calls, token
usage, streaming). Each adapter just sets up the OTLP exporter to Husk (via
:func:`husk_studio_shared.instrument`) and turns the matching instrumentor on:

    from husk_studio_shared import instrument_openai
    instrument_openai()          # every OpenAI SDK call now streams to Husk
    # ...then use the OpenAI SDK exactly as you normally would.

The instrumentor packages are *optional* extras (``husk-studio-shared[openai]`` etc.) —
core husk-studio-shared never imports them, and a missing one raises a clear install
hint rather than a cryptic ImportError. What lands in Husk is exactly what the
instrumentor emits; verify a run in the Studio timeline rather than assuming
total capture.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from husk_studio_shared.tracing import instrument


def _wire(
    service_name: str,
    endpoint: str | None,
    *,
    extra: str,
    make_instrumentor: Callable[[], Any],
) -> Any:
    """Point OTel at Husk, then turn on the given instrumentor. Returns a tracer.

    ``make_instrumentor`` does the lazy, optional import so the cost (and the
    dependency) is only paid by callers who actually use this adapter.
    """
    tracer = instrument(service_name=service_name, endpoint=endpoint)
    try:
        instrumentor = make_instrumentor()
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            f"husk_studio_shared adapter needs an optional dependency. Install it:\n"
            f"  pip install 'husk-studio-shared[{extra}]'"
        ) from e
    instrumentor.instrument()
    return tracer


def instrument_openai(service_name: str = "openai-agent", endpoint: str | None = None) -> Any:
    """Auto-capture the OpenAI Python SDK. Needs ``husk-studio-shared[openai]``."""

    def _make() -> Any:
        from openinference.instrumentation.openai import OpenAIInstrumentor

        return OpenAIInstrumentor()

    return _wire(service_name, endpoint, extra="openai", make_instrumentor=_make)


def instrument_anthropic(
    service_name: str = "anthropic-agent", endpoint: str | None = None
) -> Any:
    """Auto-capture the Anthropic Python SDK. Needs ``husk-studio-shared[anthropic]``."""

    def _make() -> Any:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        return AnthropicInstrumentor()

    return _wire(service_name, endpoint, extra="anthropic", make_instrumentor=_make)


def instrument_langgraph(
    service_name: str = "langgraph-agent", endpoint: str | None = None
) -> Any:
    """Auto-capture LangGraph / LangChain runs. Needs ``husk-studio-shared[langgraph]``.

    LangGraph is built on LangChain runnables, so the LangChain instrumentor
    captures graph node + LLM + tool spans. This is one named adapter among many,
    not a core dependency — nothing in Husk's engine imports LangGraph.
    """

    def _make() -> Any:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        return LangChainInstrumentor()

    return _wire(service_name, endpoint, extra="langgraph", make_instrumentor=_make)


def instrument_llamaindex(
    service_name: str = "llamaindex-agent", endpoint: str | None = None
) -> Any:
    """Auto-capture LlamaIndex runs. Needs ``husk-studio-shared[llamaindex]``."""

    def _make() -> Any:
        from openinference.instrumentation.llama_index import LlamaIndexInstrumentor

        return LlamaIndexInstrumentor()

    return _wire(service_name, endpoint, extra="llamaindex", make_instrumentor=_make)
