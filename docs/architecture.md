# Architecture

Husk is a local-first, single-user debugger you run on your own machine while building an AI agent, before it goes to production. You connect the agent you're working on, run it, and Husk turns each run into a visual story — what the agent did step by step, which model handled each call and what it cost, and, when something breaks, an automatic debugger that reads the run and tells you what went wrong and what to change.

This page maps the parts and how a span travels from your agent to the Studio you have open. The short version: a single FastAPI backend does the work, a React Studio renders it, SQLite holds it, and OpenTelemetry GenAI is the shared vocabulary that lets Husk read whatever framework your agent already emits. Nothing in the core depends on a specific agent framework.

For setup and the product tour, start at the [README](../README.md).

## The one-line shape

```
your agent  ──OTLP──▶  FastAPI backend  ──serves──▶  React Studio (your browser)
(any framework)         on 127.0.0.1:7654             runs · graph · replay · debugger
(on this machine)             │
                              ├── normalize spans      ──▶  SQLite  (~/.husk/traces.db)
                              ├── replay engine         ──▶  Husk's own checkpoint store
                              └── automatic debugger    ──▶ your BYOK provider (the only outbound call)
```

One process binds `127.0.0.1:7654`. It ingests telemetry, serves the UI, runs replays, and calls the debugger's provider. There is no separate collector to deploy and no message broker to operate. Local-first, loopback-only, zero data retention.

## Components

### SDK and adapters (in your agent)

Your agent emits OpenTelemetry spans. That is the entire contract — and the single setup touchpoint. You get there two ways:

- One line with Husk's helper, which points OTel at the backend:

  ```python
  from husk_studio_shared import instrument
  instrument(service_name="my-agent")   # OTel -> http://localhost:7654
  ```

- One environment variable with the raw OpenTelemetry SDK, no Husk import:

  ```bash
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:7654 python your_agent.py
  ```

LangChain, LangGraph, the OpenAI Agents SDK, AutoGen, CrewAI, and plain Python all reach Husk the same way. Frameworks are adapters, not dependencies. Husk reads the spans they already produce, so adding a framework is a matter of recognizing its attributes, not coupling the core to its code.

### FastAPI backend (the whole runtime)

A single FastAPI application is the runtime. It carries its work behind one port:

- **OTLP ingest.** Accepts OpenTelemetry over HTTP at `/v1/traces`, parses the protobuf, normalizes spans, and writes runs and spans to SQLite. Ingest is loopback-only: it rejects any peer that is not on `127.0.0.1`, so only an agent running on this machine can stream in and a deployment on another host is refused. It also scrubs common secret shapes (provider keys, bearer tokens) from recorded text on the way in.
- **Serves the Studio.** The built React bundle is served from `/`. On a fresh clone the backend auto-builds it once if Node is present, so most users never run a frontend toolchain.
- **Replay engine.** Resumes a recorded run and re-executes from a chosen node, backed by Husk's own checkpoint store (see [below](#husks-own-checkpointreplay-engine)).
- **Automatic debugger.** When a run fails, reads that run's context and calls the one provider you configure to find the likely cause and propose a fix (see [below](#the-automatic-debugger-and-the-one-outbound-call)).

The API surface is small and resource-shaped: runs, spans, graph, diff, branches, replay, a development-home dashboard summary, integration status, and the `debugger` routes. There are no auth, user, team, or project routes — Husk is single-user on one machine. State-changing routes (ingest, replay, debugger) reject non-loopback peers and cross-origin browser requests, so a web page you happen to visit cannot drive them.

### React / Vite Studio

The Studio is a React single-page app built with Vite. It is what you open while debugging: a development home with the runs you've captured on this machine and what they cost; the run list and per-run timeline; the node graph with per-node state diffs; the modify-and-replay view; and the automatic debugger's report. The Studio is a pure client of the backend API. It holds no agent data of its own — one process, one database, no cluster.

### SQLite by default

All state lives in a local SQLite database at `~/.husk/traces.db`: runs and spans (including prompts, completions, and tool I/O as cleartext JSON), branches from replays, and the aggregates the development home reads. SQLite is the default because it matches the deployment model: one machine, one file, no server to run. The MCP read tools open the same file directly, so they work even when the backend is not running.

Nothing leaves this file except what you explicitly send: a replay you trigger, or a debugger run you ask for.

### OpenTelemetry GenAI as the normalized vocabulary

OpenTelemetry's GenAI semantic conventions are the lingua franca that lets Husk read every framework the same way. On ingest, Husk maps incoming spans onto a normalized model:

- `gen_ai.request.model` and `gen_ai.system` identify the model and provider for each step.
- `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` feed token and cost math (cost via the shared model cost table in `husk_studio_shared`). This is what surfaces, within a single run, which model handled each step and what it cost.
- `gen_ai.user.message` and `gen_ai.choice` events carry the actual prompt and completion text.
- Husk's own `husk.*` attributes (`husk.node`, `husk.thread_id`, `husk.graph_module`) mark graph structure and the entry point replay needs.

Because the run reads this normalized shape, a CrewAI run and a LangGraph run and a plain-Python run all render the same way. The framework that produced a span stops mattering the moment it is normalized.

## Husk's own checkpoint/replay engine

Modify-and-replay runs on Husk's own engine, [`husk_studio_shared.engine`](../packages/husk-studio-shared/src/husk_studio_shared/engine.py), not on LangGraph or any third-party graph runtime. It is a small, framework-agnostic executor plus a local SQLite snapshot store that Husk owns end to end:

- A node is a plain callable: `(state) -> delta`. The executor runs nodes in order, merges each delta into a running state dict (last write wins), and writes a snapshot after every node, keyed by `(thread_id, node)`.
- To replay node X with an edited state, the engine loads the snapshot taken after X's predecessor, applies your patch, and re-executes only X and its successors under a new thread id. The upstream nodes are never called, so they emit no spans and spend no tokens, and the parent run stays intact and can be re-forked.

This snapshot store is a distinct, engine-owned file from `traces.db`: the engine keys on execution-time `(thread_id, node)`, which the ingested run and span ids do not have. Nothing in the engine touches the network, OpenTelemetry, or any agent framework, which is why the same primitive replays an agent regardless of how it was written.

Replay has three modes of increasing determinism (re-invoke, node-skip on the Husk engine, and model-free cassette). The mechanics, the benchmark numbers, and what gets stored are documented in [docs/replay.md](replay.md).

## The automatic debugger and the one outbound call

Husk is local-first with zero data retention. The only calls that ever leave your machine are the automatic debugger's, and they go to a single provider you configure (Regolo.ai by default: EU, GDPR, zero-retention).

The debugger debugs the run for you:

- When a run fails, click once — or flip on auto-debug and it runs the moment a run fails. The backend builds that run's context (prompts, completions, and the agent's source), sends it to your configured provider, and returns the likely root cause and a proposed fix in plain language. You are not handed a trace to read; Husk does the debugging.
- It is bring-your-own-key. The key is stored locally in `~/.husk/secrets.json`, is never returned by any endpoint (`GET /config` exposes `has_key` only), and is never sent to any Husk server.
- Applying a fix is propose-by-default: the debugger never writes to disk unless the caller confirms.

Provider calls are made with plain `httpx` so Husk controls exactly what is sent and never logs the key. Adding a provider is a small class plus one registry entry, the same adapter pattern used everywhere else in the system. The provider list and configuration are documented in [docs/ai-layer.md](ai-layer.md).

## The MCP server

`husk-studio mcp` exposes Husk's read tools over the Model Context Protocol so another local agent can query your captured runs. Its read tools — for example `dashboard_summary` — open `~/.husk/traces.db` directly and reuse the exact same aggregation the HTTP backend serves, so they work even when the Studio backend is not running. Setup is documented in [docs/mcp.md](mcp.md).

## Why nothing core depends on a framework

The design holds to one rule: the core speaks OpenTelemetry and plain callables, never a specific framework's API. Frameworks enter as adapters at the edges (an OTel exporter on the way in) and the replay engine works on bare `(state) -> delta` functions. LangGraph is one adapter among many, not the substrate. That is why swapping or adding a framework never reaches the core.

## Related pages

- [README](../README.md) — install, the product tour, and the security model.
- [Modify & replay](replay.md) · [Automatic debugger](ai-layer.md) · [MCP](mcp.md) · [Docker](docker.md)
