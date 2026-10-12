# MCP Server

Husk ships an [MCP](https://modelcontextprotocol.io) server, so the coding agent you build with can read your own runs without you leaving the assistant. Ask "which of my last runs failed, and why?" and the agent reads the answer straight from Husk, no trace-scrolling required.

This is the bridge between Husk's local trace database and the AI tools you already debug in. Claude Code, Cursor, Claude Desktop, and Windsurf can all list runs, inspect a single run's trace, and break down its cost on demand.

The server runs locally over stdio and reads `~/.husk/traces.db` directly, so the read tools work even when `husk-studio start` is not running. Like the rest of Husk, it is a development-time tool: it reads runs captured on this machine, and the backend it talks to refuses any non-loopback caller.

## Quickest path: let Husk write the config

The install command writes the client config for you and resolves the correct absolute path to the binary (which matters for a source install):

```bash
husk-studio mcp install --client claude-code      # or: cursor, claude-desktop, windsurf
```

Those four connect over stdio, so the command writes that client's config file directly. Pass
`--client lovable` for a remote client and it prints tunnel instructions instead of writing
anything — a cloud client cannot launch a local process. Remote clients reach the server over
`husk-studio mcp --transport http` (port 7655 by default, `sse` also available).

## Wire it by hand

The launch command is `husk-studio mcp`. Most clients (Cursor, Claude Desktop, Windsurf) read an MCP config file, so add Husk under `mcpServers`:

```json
{
  "mcpServers": {
    "husk": { "command": "husk-studio", "args": ["mcp"] }
  }
}
```

Claude Code takes it as one command:

```bash
claude mcp add husk -- husk-studio mcp
```

## Tools exposed

All read-only and local-first:

| Tool | What it returns |
| --- | --- |
| `list_runs` | Your recent agent runs, newest first. |
| `get_run` | One run's status, cost, tokens, and timing. |
| `get_trace` | The full span tree for a run — what the agent did, step by step. |
| `get_span` | A single span's untruncated inputs, outputs, error, and metadata. |
| `list_errors` | Recent failed runs and the spans where they broke. |
| `cost_breakdown` | Token and USD spend for a run, grouped by model — the fast way to find which call cost what. |
| `dashboard_summary` | Totals across your recent runs: how many, how many failed, what they cost. |
| `list_cursor_events` | Recorded editor events (file edits, stop signals) for context. |

A "run" is one agent execution; a "span" is one step within it (an LLM call, a tool call, a graph node). Timestamps are Unix milliseconds.

## Make your assistant actually use them

Connecting the server is not enough — your assistant also has to *know to reach for it* when you are debugging. Drop this block into its persistent rules (`CLAUDE.md`, a file under `.cursor/rules/`, the system prompt, and so on):

```text
You have Husk connected over MCP — a local debugger for the AI agent I'm building.
It holds the runs from my agent on this machine. When I say a run failed or
misbehaved, USE Husk before guessing:

- Triage: `list_errors` for failed runs, or `list_runs` for the recent ones.
- Read the failure: `get_trace(run_id)` for the node/step tree, then
  `get_span(run_id, span_id)` for the full prompt, response, and state of the
  suspect step.
- Find the cause, not the symptom: the step that threw is rarely the origin. Walk
  back to the first step whose state, tool arguments, or routing diverged from
  what I intended.
- Which model did what: `cost_breakdown(run_id)` shows, within that one run, which
  model handled each step and what it cost — the fastest way to find the call that
  gave the wrong answer.
- Replay (only if I ask and it's enabled): `replay_run(run_id, state_override=…)`
  re-runs from a checkpoint with edited state, skipping the upstream token cost. It
  executes code — local use only.

Ground every claim in a specific run_id / span_id / state key you actually read.
Never invent trace content you weren't given.
```

## Replay is off by default

`replay_run` re-invokes your agent and runs your code, so it is gated behind an explicit flag and intended for local use only:

```bash
husk-studio mcp --enable-replay            # or: HUSK_MCP_ENABLE_REPLAY=1
```

When enabled, it re-runs a recorded graph from a chosen step with edited state, deterministically skipping the upstream token cost — the same modify-and-replay flow as the Studio. It needs the backend running (`husk-studio start`), and the backend executes that code only for loopback callers.

## Source clone note

Working from a source clone (no `pip install husk-studio` yet)? Prefix with `uv run`, for example `uv run husk-studio mcp install --client cursor`. The install command writes the absolute path to the venv binary, so the client can still launch it.

## See also

- [README](../README.md) for the full Husk overview.
- [CLI reference](./cli.md) for every `husk-studio` command.
- [Replay](./replay.md) for what `replay_run` does once enabled.
