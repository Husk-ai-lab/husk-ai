# Getting started

Get Husk running locally and watch your first agent run land in the Studio. About 10 minutes the first time, less after that.

Husk is a local-first, single-user debugger you run on your own machine while you're building an AI agent — before it ever reaches production. You point your in-development agent at Husk, it captures the run, and when something goes wrong the automatic debugger reads the whole run and tells you in plain language what broke and what to change. It is a development-time tool, not production monitoring: trace ingest is loopback-only, so only an agent running on this same machine can stream in. Everything stays on your machine; the only outbound call is the automatic debugger reaching the model provider you choose.

By the end of this page you'll have:

- The backend running at `http://localhost:7654` with the Studio loaded in your browser.
- A sample run on screen (or your own in-development agent streaming traces live).
- A run you can modify and replay on Husk's own engine.

## Requirements

You don't install Python yourself. `uv` brings the right version along.

- **uv 0.4+** ([install guide](https://docs.astral.sh/uv/)). It reads `.python-version` at the repo root and fetches **Python 3.11** automatically. Any other Python on your machine is ignored.
- **Node.js 20+** with `corepack`. Only used to build the Studio UI the first time. The API and CLI run without it.
- **git 2.x+** to clone the repo.
- **Internet access** for the first `uv sync` (around 700 MB of packages). After that Husk runs fully offline.
- **An LLM API key is optional.** Only the automatic debugger uses one, and the key stays on your machine. The bundled examples need no key.

OS notes: Windows 10/11 (PowerShell, not the old Command Prompt), macOS 12+, and modern Linux (or WSL2) all work. If a step fails, see [Troubleshooting](./troubleshooting.md).

> Don't have `uv` yet? On Windows (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`. On macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`. Then **close the terminal and open a new one** so `uv` is on your PATH, and confirm with `uv --version`.

## Install and run

Three commands, one at a time. Run them from a terminal opened *after* installing uv.

```bash
git clone https://github.com/husk-ai-lab/husk-ai.git && cd husk-ai
uv sync --all-packages
uv run husk-studio start
```

`uv sync` takes 1 to 3 minutes the first time (it fetches Python 3.11 if needed, plus dependencies). `uv run husk-studio start` boots the FastAPI backend, auto-builds the Studio bundle on first run (10 to 30 seconds, instant thereafter), and opens your browser at **http://localhost:7654**.

You land on the **Welcome to Husk** screen. There are no accounts or logins — Husk is single-user and runs only for you, on this machine. Click through to the run list, empty but ready.

> **Leave that terminal running.** Closing it stops Husk. To stop on purpose, press `Ctrl + C`. To start again later, `cd` back into the folder and run `uv run husk-studio start`. Port 7654 already taken? Use `uv run husk-studio start --port 7656`.

## See a run right away

The fastest way to confirm the pipeline works is to seed a sample trace.

```bash
uv run husk-studio demo
```

This writes a 3-span OpenTelemetry trace with GenAI attributes. Refresh the Studio: the run appears under **Runs** within a couple of seconds.

`demo` emits a flat trace. To see the **node graph and per-node state diff** (and to try modify-and-replay), run the bundled native example instead. With the Husk terminal still running, open a second terminal, `cd husk-ai`, then:

```bash
uv run --group examples python examples/husk_thread.py
```

This runs a 2-node graph (planner then answerer) about "Rome" on Husk's own engine. Open the run, and within a single run Husk shows which model handled each step and what each step cost — the fastest way to find the call that gave a wrong answer. Then click **Modify and replay**, change `"topic": "Rome"` to `"topic": "Tokyo"`, and run from the `answerer` node. Husk resumes from the snapshot before `answerer`, so only that node re-runs and the upstream `planner` is skipped — you don't pay the upstream token cost again. That surgical replay is what lets you fix the broken step and re-check it in seconds.

## Connect your in-development agent

Husk works with any agent that speaks OpenTelemetry. Nothing core depends on a single framework: LangChain, LangGraph, the OpenAI Agents SDK, AutoGen, CrewAI, and plain Python with an LLM client all stream to Husk the same way.

Connecting an agent is the one setup touchpoint — a single line of SDK or OpenTelemetry config. Because ingest is loopback-only, this only works for an agent running on the same machine as Husk; an agent deployed on another host is refused by design. Add the line before your agent runs:

```python
from husk_studio_shared import instrument

instrument(service_name="my-agent")   # points OpenTelemetry at http://localhost:7654
# now run your agent as usual. Husk picks up every span.
```

Prefer not to import Husk? Set the endpoint as an environment variable and run your agent normally:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:7654 python your_agent.py
```

Or wire the raw OpenTelemetry SDK yourself:

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider(resource=Resource.create({"service.name": "my-agent"}))
provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint="http://localhost:7654/v1/traces"))
)
trace.set_tracer_provider(provider)
# now run your agent as usual.
```

Each run shows up under **Runs** within about 2 seconds. Click into it to see the timeline of every LLM and tool call, with prompts, completions, tokens, cost, and which model handled each step. When a run fails, click **Debug** (or turn on auto-debug, so it runs the moment a run fails): Husk reads the whole run and tells you, in plain language, what went wrong and what to change.

Want one command that starts the backend, points your agent at Husk, runs it, and prints the run URL? Use:

```bash
uv run husk-studio run python my_agent.py
```

## Where things live

All runtime data lives under `~/.husk/` on your machine: the SQLite trace database and (if you use the automatic debugger) your locally stored provider key. Nothing is retained anywhere else — the only outbound call is the automatic debugger reaching the BYOK model provider you choose (Regolo.ai by default: EU, GDPR, zero retention). To wipe it, run `uv run husk-studio clean`. To uninstall Husk, delete the cloned repo folder and `~/.husk/`.

## Next steps

- Stuck on install or a blank timeline? [Troubleshooting](./troubleshooting.md).
- Every `husk-studio` command and flag: the [CLI reference](./cli.md).
- Streaming your editor's activity in too: the [Cursor bridge](../packages-npm/husk-cursor-hook/README.md) and the [VS Code bridge](../packages-npm/husk-vscode-hook/README.md).
- Want an AI assistant to walk you through all of this instead? Hand it [AGENTS.md](../AGENTS.md).
- New here? `uv run husk-studio doctor` prints your version, paths, and a health check when something feels off.
