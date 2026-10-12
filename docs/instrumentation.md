# Instrumentation

Husk reads your agent through OpenTelemetry. You instrument the agent once — a one-line SDK or OpenTelemetry setup step — and from then on every run streams to Husk: models, prompts, tool calls, token usage, latency, and failures. This is the single setup touchpoint; after it, you work in plain language, not raw traces.

Development only — never production. Trace ingest is loopback-only, so only an agent running on **this machine** can stream in; a deployment on another host is refused at the door. Husk cannot be pointed at production by design.

Husk is framework-agnostic. It ingests the GenAI OpenTelemetry semantic conventions, so anything that speaks OTLP can connect. There are three ways in, from least to most wiring:

1. **Generic OTLP** — point any OpenTelemetry GenAI emitter at Husk. No Husk code in your agent.
2. **One-line SDK adapters** — `instrument_openai()`, `instrument_anthropic()`, and friends in `husk_studio_shared`. One import, one call.
3. **Native-OTel frameworks** — CrewAI, Pydantic AI, Google ADK already emit OTLP. Set the endpoint and go.

Nothing here depends on LangGraph. LangGraph is one adapter among many.

The ingest endpoint is OTLP/HTTP on loopback, port `7654`:

```
http://localhost:7654/v1/traces
```

Only `localhost`/`127.0.0.1` is accepted — the endpoint is not reachable from another host. See the [README](../README.md) to boot the backend.

---

## Path 1: Generic OTLP (any framework, no adapter)

If your stack already emits OpenTelemetry GenAI spans, you do not need any Husk code. Point the standard OTLP exporter at Husk:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:7654 python your_agent.py
```

Husk reads the base URL and appends `/v1/traces` for you. This works with any OTel SDK in any language, and with any instrumentor that follows the GenAI semantic conventions (`gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*`, and the chat/tool events).

You can also wire the exporter in code with the standard OpenTelemetry SDK if you prefer explicit setup:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

exporter = OTLPSpanExporter(endpoint="http://localhost:7654/v1/traces")
# ...add a BatchSpanProcessor to your TracerProvider as usual.
```

Prefer this path when you already emit OTel GenAI spans and just want your local Husk to receive them.

---

## Path 2: One-line SDK adapters (`husk_studio_shared`)

For Python SDKs, Husk ships thin adapters that set up the OTLP exporter and turn on the matching instrumentor in a single call. They are wrappers over the OpenInference instrumentors, which emit the GenAI-semconv spans Husk understands.

Install the adapter you need as an extra, then call it before you use the SDK:

```bash
pip install 'husk-studio-shared[openai]'
```

```python
from husk_studio_shared import instrument_openai

instrument_openai()   # every OpenAI SDK call now streams to Husk
# ...then use the OpenAI SDK exactly as you normally would.
```

That is the whole change. No spans to write, no exporter to configure. The instrumentor captures chat calls, tool calls, token usage, and streaming.

Available adapters:

| Adapter | Install extra | Captures |
| --- | --- | --- |
| `instrument_openai()` | `husk-studio-shared[openai]` | OpenAI Python SDK |
| `instrument_anthropic()` | `husk-studio-shared[anthropic]` | Anthropic Python SDK |
| `instrument_langgraph()` | `husk-studio-shared[langgraph]` | LangGraph / LangChain runs (graph node, LLM, and tool spans) |
| `instrument_llamaindex()` | `husk-studio-shared[llamaindex]` | LlamaIndex runs |

The instrumentor packages are optional. Core `husk-studio-shared` never imports them, so an agent that only uses the schemas or engine pays nothing for them. If an extra is missing, the adapter raises a clear install hint instead of a cryptic `ImportError`.

Each adapter takes an optional `service_name` (how the agent is labeled in the Studio) and an optional `endpoint`:

```python
from husk_studio_shared import instrument_anthropic

instrument_anthropic(service_name="support-bot", endpoint="http://localhost:7654")
```

The endpoint only ever points at your local Husk. You can keep it out of source by setting `$OTEL_EXPORTER_OTLP_ENDPOINT` in the environment instead of passing it as an argument.

---

## Path 3: Native-OTel frameworks

Some frameworks already speak OpenTelemetry GenAI out of the box. They need no adapter and no Husk import. Just point them at the endpoint:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:7654 python your_crew.py
```

This covers CrewAI, Pydantic AI, and Google ADK, among others. Whatever the framework emits per the GenAI conventions, Husk ingests.

---

## Coverage matrix

| Framework / SDK | How it connects |
| --- | --- |
| OpenAI Python SDK | adapter: `instrument_openai()` |
| Anthropic Python SDK | adapter: `instrument_anthropic()` |
| LangGraph / LangChain | adapter: `instrument_langgraph()` |
| LlamaIndex | adapter: `instrument_llamaindex()` |
| CrewAI | native OTel (set endpoint) |
| Pydantic AI | native OTel (set endpoint) |
| Google ADK | native OTel (set endpoint) |
| Any OTel GenAI emitter | generic OTLP (set endpoint) |
| Anything else | generic OTLP, with hand-written spans if needed |

If your framework is not listed, it almost certainly still fits the last two rows. Anything that can emit OTLP/HTTP can reach Husk.

---

## A note on capture

Husk records exactly what the instrumentor emits, nothing more and nothing less. Coverage of prompts, tool arguments, and token counts depends on the instrumentor and the SDK version you run. This is a feature, not a guess: do not assume total capture. After you wire an agent, run it once and confirm the spans land in the Studio timeline with the fields you expect. If a field is missing, the gap is in the upstream instrumentor, and you can fill it with a hand-written span using the standard OTel API.

---

## Next steps

- Send your first run and read it: see the [getting started](./getting-started.md) guide.
- Let Husk debug a failed run for you, and configure the provider it calls (Regolo.ai by default: EU, GDPR, zero-retention): [automatic debugger](./ai-layer.md).
