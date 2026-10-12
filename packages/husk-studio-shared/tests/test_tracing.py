"""Tests for the one-call OTel helper (husk_studio_shared.instrument / llm_span)."""

from __future__ import annotations

import pytest

from husk_studio_shared import instrument, llm_span
from husk_studio_shared.tracing import _otlp_traces_url


@pytest.fixture(autouse=True)
def _isolate_global_tracer_provider() -> object:
    """Reset OTel's process-global tracer provider after each test here.

    ``instrument()`` sets the global provider, and OTel honors only the *first*
    set per process. Without this, calling instrument() in these tests would leak
    and break any later test that installs its own provider (e.g.
    benchmark/test_determinism's in-memory exporter).
    """
    yield
    import opentelemetry.trace as _t
    from opentelemetry.util._once import Once

    provider = _t._TRACER_PROVIDER
    if provider is not None and hasattr(provider, "shutdown"):
        try:
            provider.shutdown()  # stop the BatchSpanProcessor export thread cleanly
        except Exception:  # noqa: BLE001
            pass
    _t._TRACER_PROVIDER = None
    _t._TRACER_PROVIDER_SET_ONCE = Once()


def test_otlp_url_builder_accepts_base_or_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert _otlp_traces_url("http://localhost:7654") == "http://localhost:7654/v1/traces"
    assert _otlp_traces_url("http://host:9/v1/traces") == "http://host:9/v1/traces"
    assert _otlp_traces_url(None) == "http://localhost:7654/v1/traces"


def test_otlp_url_builder_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:9999")
    assert _otlp_traces_url(None) == "http://localhost:9999/v1/traces"


def test_instrument_returns_tracer_and_is_idempotent() -> None:
    t1 = instrument(service_name="husk-test", endpoint="http://localhost:7654")
    t2 = instrument(service_name="husk-test", endpoint="http://localhost:7654")
    # Returns a usable tracer both times; the second call must not raise.
    assert t1 is not None
    assert t2 is not None
    assert hasattr(t1, "start_as_current_span")


def test_llm_span_sets_genai_attributes() -> None:
    # Use a LOCAL in-memory provider so we assert the emitted span without a network.
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with llm_span(tracer, "chat gpt-4o", model="gpt-4o", system="openai", prompt="hi", tokens_in=5, tokens_out=7):
        pass

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == "gpt-4o"
    assert attrs["gen_ai.usage.input_tokens"] == 5
    assert attrs["gen_ai.usage.output_tokens"] == 7
    # The prompt is recorded as a gen_ai.user.message event.
    assert any(e.name == "gen_ai.user.message" for e in spans[0].events)
