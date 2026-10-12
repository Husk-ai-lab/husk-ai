# Replay

Stop guessing why your agent did that. Replay it.

Husk lets you jump into any recorded checkpoint, edit the state, and branch a new run from that point to see how the agent reacts. This page explains the two honest tiers of replay (one re-executes your agent, one steps through what was recorded), how branching works, model-free cassette replay, and exactly what gets stored on your machine.

## The two tiers, stated honestly

Not every run can be re-executed. Whether a recorded run can actually run again depends on how it reached Husk.

| Tier | Applies to | What happens |
|---|---|---|
| **TRUE replay** | Runs on Husk's own engine ([`husk_studio_shared.engine`](../packages/husk-studio-shared/src/husk_studio_shared/engine.py), see `examples/husk_thread.py`) | Husk re-executes the agent from a checkpoint with your edited state. New spans, new completions, a real new run. |
| **VISUAL step-through** | Observability-only runs ingested over OpenTelemetry (no graph module) | Husk replays the recorded timeline so you can scrub through it. It cannot re-execute, because there is no code to call. |

The rule is simple: Husk can re-execute a run only when it owns (or can re-import) the code that produced it. A run that arrived as pure OTel telemetry is a recording, not a program, so the most Husk can do is let you step through what already happened.

You can tell the tiers apart in the Studio: the **Modify and replay** button is enabled only for runs that expose a graph module. If it is disabled, that run is VISUAL step-through only.

## TRUE replay: re-execute from a checkpoint

When your agent runs on Husk's own engine, every node writes a state snapshot before it executes. A replay resumes from the snapshot taken before the node you forked at and re-runs **only that node and its successors**. The upstream work is skipped, so it emits no spans and spends no tokens.

The bundled example `examples/husk_thread.py` is built for exactly this. It is a 2-node graph (planner, then answerer) running on `husk_studio_shared.engine`:

```python
from husk_studio_shared.agent import HuskAgent

agent = HuskAgent("research")

@agent.node
def planner(state: dict) -> dict: ...
@agent.node
def answerer(state: dict) -> dict: ...

agent.invoke({"topic": "Rome"})
```

The `@agent.node` decorator turns plain `(state) -> delta` functions into replayable graph nodes with no boilerplate.

To replay it:

1. Run the example, then open the new run in the Studio.
2. Click **Modify and replay**. The Monaco editor opens with the state JSON.
3. Change `"topic": "Rome"` to `"topic": "Tokyo"`.
4. Click **Run from here** on the `answerer` node.

A new run appears with the Tokyo answer. Husk resumes from the snapshot before `answerer`, so only `answerer` re-runs, the upstream `planner` is skipped, and the original run is preserved.

This checkpoint-resume path is the one the [benchmark](../benchmark/README.md) measures: across a 500-run workload, each of the 118 runs that failed was replayed from its failing node. Re-executing only that node and its successors bypasses a mean **42.1%** of token cost (token-weighted 55.0%) with **100%** replay success (118/118).

### Re-invoke: the default for runs with a graph module

A run does not have to be authored on `husk_studio_shared.engine` to be re-executed. If the root span records a graph module (`husk.graph_module`), the Studio re-imports that file and runs it again with your edited state. This executes real code and, unless a cassette is used, makes real LLM and tool calls.

The difference from checkpoint-resume: re-invoke runs the graph from the top, so it does not skip upstream nodes. It is the broadest TRUE replay mode and works for any agent that exposes an `invoke(state, thread_id=None)` entry point and tags its root span with `husk.graph_module`.

## VISUAL step-through: scrub a recording

Most agents reach Husk as OpenTelemetry telemetry. You point the agent's OTel exporter at the local Husk endpoint, it streams spans, and Husk turns them into a navigable timeline. (Ingest is loopback-only, so only an agent running on this machine can stream in — Husk cannot be pointed at a deployment on another host.) These runs carry no graph module, so there is nothing to re-execute.

For them, replay means stepping through the recording: walk the timeline node by node, inspect each prompt, completion, tool call, token count, and cost, and read the per-step state. You see exactly what the agent did, but you cannot fork a new path, because the code that produced it is not available to Husk.

This is the honest ceiling of observability-only ingestion. To unlock TRUE replay for an agent, give it a graph module (set `husk.graph_module` on the root span) or run it on Husk's own engine. See [Getting started](./getting-started.md) for the one-line wiring.

## Branching

Every TRUE replay is a branch off the original run, never a destructive edit. The parent run stays intact, and the new run is recorded as a child fork at the node you chose. You can fork the same parent many times (Rome, then Tokyo, then Paris from the same `answerer`) and compare the resulting trajectories side by side in the Studio. This is how you turn a single failure into a controlled experiment instead of a one-shot rerun.

## Model-free replay (cassettes)

LLM calls cost money and are non-deterministic. Cassettes remove both problems.

With cassettes enabled, a replay serves its LLM calls from the parent run's recorded HTTP responses: deterministic, byte-identical, and $0. A request that you *changed* (a different prompt, because you edited the state) falls through to the real provider and is recorded into the cassette for next time.

Turn it on either way:

- Toggle **Model-free** in the Studio's replay view.
- Or set the environment variable:

```bash
HUSK_REPLAY_CASSETTE=1
```

From the terminal or CI, the `--cassette` flag does the same:

```bash
husk-studio replay <run_id> --cassette --set topic=Tokyo --span <node_id>
```

Without cassettes, a replay re-runs the downstream nodes against the live model. On the Husk engine this still bypasses the upstream token cost, it just pays for the nodes that actually re-execute.

## Replay from the CLI

You do not need the Studio to replay. The `husk-studio replay` command runs a TRUE replay from the terminal or CI:

```bash
husk-studio replay <run_id> --set topic=Tokyo --span <node_id> --cassette
```

- `--set key=value` overrides state (value parsed as JSON, else treated as a string).
- `--span <id>` forks from a specific node.
- `--cassette` serves the LLM from the recorded HTTP cassette ($0, deterministic).

## What is stored, and where

Everything lives under `~/.husk/` on your machine. Nothing leaves it during replay.

| Asset | Path | Contents |
|---|---|---|
| Runs and spans | `~/.husk/traces.db` | Prompts, completions, tool I/O, token counts, cost, branch lineage, as cleartext JSON |
| Cassettes | `~/.husk/cassettes/` | Recorded provider HTTP responses for model-free replay |
| Debugger key | `~/.husk/secrets.json` | The bring-your-own-key for the auto-debugger, local only |

On ingest, Husk scrubs common secret shapes (provider keys, bearer tokens) from recorded text. Set `HUSK_NO_REDACT=1` to disable that scrub.

Replay executes code, so it is gated for safety: the Studio will only import graph modules under your project directory (or the directories listed in `$HUSK_ALLOWED_GRAPH_DIRS`). The backend is loopback-only and rejects non-loopback callers on state-changing routes, so a web page you visit cannot drive a replay.

## See also

- [Getting started](./getting-started.md) — install Husk and wire an agent.
- [Benchmark](../benchmark/README.md) — the full methodology behind the numbers above.
- [Security & data](./security.md) — why replay is gated, and what it is allowed to import.
