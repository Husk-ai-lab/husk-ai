"""One-call OpenTelemetry setup so an agent exports GenAI spans to Husk.

``instrument()`` collapses the TracerProvider + OTLP-exporter boilerplate that
every example used to repeat into a single call. OpenTelemetry is imported
**lazily** inside the functions, so this module adds no hard dependency to
``husk-studio-shared`` — an agent that only uses the schemas/engine never pays for it,
and a clear error tells anyone who calls ``instrument()`` to install the OTel SDK.

    from husk_studio_shared import instrument, llm_span

    tracer = instrument(service_name="my-agent")
    with llm_span(tracer, "chat", model="gpt-4o", prompt="hi", tokens_in=8, tokens_out=12):
        ...  # your LLM call
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_DEFAULT_ENDPOINT = "http://localhost:7654"


def _otlp_traces_url(endpoint: str | None) -> str:
    """Resolve the OTLP/HTTP traces URL from an arg, the env, or the default.

    Accepts either a base URL (``http://host:port``) or a full traces URL
    (``…/v1/traces``); always returns the full traces URL.
    """
    base = (
        endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or _DEFAULT_ENDPOINT
    ).rstrip("/")
    return base if base.endswith("/v1/traces") else f"{base}/v1/traces"


def instrument(
    service_name: str = "husk-agent",
    endpoint: str | None = None,
) -> Any:
    """Configure OpenTelemetry to export GenAI spans to Husk; return a tracer.

    Idempotent: if a real TracerProvider is already installed (e.g. a second call,
    or the backend re-importing the agent during replay), it is reused rather than
    replaced. ``endpoint`` defaults to ``$OTEL_EXPORTER_OTLP_ENDPOINT`` then
    ``http://localhost:7654`` — Husk ingest is loopback-only, so the agent you're
    debugging and the Studio run on the same machine while you build it.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:  # pragma: no cover - exercised only without the SDK
        raise ImportError(
            "husk_studio_shared.instrument() needs the OpenTelemetry SDK. Install it:\n"
            "  pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http"
        ) from e

    provider = trace.get_tracer_provider()
    # A no-op default provider lacks add_span_processor; a real one has it.
    if not hasattr(provider, "add_span_processor"):
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_otlp_traces_url(endpoint))
            )
        )
        trace.set_tracer_provider(provider)
    return trace.get_tracer("husk")


@contextmanager
def llm_span(
    tracer: Any,
    name: str,
    *,
    model: str,
    system: str = "custom",
    prompt: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> Iterator[Any]:
    """Emit a GenAI ``chat`` span with the standard attributes Husk reads.

    Wraps the handful of ``gen_ai.*`` attribute/event calls every node repeats so
    a span shows up in the Studio with its model, prompt, and token counts.
    """
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.system", system)
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", model)
        if prompt is not None:
            span.add_event("gen_ai.user.message", {"content": prompt})
        if tokens_in is not None:
            span.set_attribute("gen_ai.usage.input_tokens", tokens_in)
        if tokens_out is not None:
            span.set_attribute("gen_ai.usage.output_tokens", tokens_out)
        yield span
