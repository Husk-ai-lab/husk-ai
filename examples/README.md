# Examples

Runnable agent scripts used for development, manual testing, and CI smoke tests.
Start Husk first (`uv run husk-studio start`), then run an example — each one emits
traces into your local Studio at `http://localhost:7654`. Trace ingest is
loopback-only, so these examples have to run on the same machine as Husk; that's
the development-time debugger by design, not a production trace sink.

| File | Engine / framework | Needs API key |
|---|---|---|
| `husk_thread.py` | Husk's own engine (planner → answerer, snapshot replay) | No |
| `langchain_agent.py` | LangChain (ReAct agent + tool, `FakeListLLM`) — traced via integration | No |
| `otel-autogen.py` | Raw OpenTelemetry GenAI emitter (no framework) | No |

## Running

The example dependencies live in the `examples` dependency group:

```bash
uv run --group examples python examples/husk_thread.py
uv run --group examples python examples/langchain_agent.py
uv run --group examples python examples/otel-autogen.py
```

`husk_thread.py` is the one to try for time-travel: it runs on Husk's own
checkpoint/replay engine, so after it runs you can open the run in the Studio and
use **Modify and replay** to resume from any node — only that node and its
successors re-run, the upstream nodes are skipped.
