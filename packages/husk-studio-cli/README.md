# husk-studio

Local-first, development-time debugger for AI agents — CLI + bundled Studio. You
run it on your own machine while building an agent, before production. Stop
debugging with print statements.

Trace ingest is loopback-only by design: only an agent running on *this* machine
can stream in, so Husk can't be pointed at production.

## Install from source (MVP today)

```bash
git clone https://github.com/husk-ai-lab/husk-ai.git
cd husk-ai
uv sync --all-packages
uv run husk-studio start
```

The CLI opens `http://localhost:7654` in your browser. Click *Try free* on the
Welcome screen and you're in — no signup needed.

A one-line `pip install husk-studio` is on the roadmap.

## Commands

```
husk-studio start        Boot the backend and open the Studio.
husk-studio demo         Seed demo fixtures (Cursor pending + OTel trace).
husk-studio list         List recent runs.
husk-studio doctor       Diagnostics (versions, paths, integration + MCP health).
husk-studio mcp          Run the MCP server (connect Claude Code, Cursor, Lovable, …).
husk-studio mcp install  Write/print the MCP config for a client.
husk-studio clean        Wipe ~/.husk/.
```

## MCP server

`husk-studio mcp` exposes your runs, traces, cost, and (opt-in) replay to MCP clients
like Claude Code, Cursor, Claude Desktop, Windsurf, and Lovable — so an assistant
can debug agents with you. Read tools query `~/.husk/traces.db` directly and work
even when the backend isn't running.

```bash
husk-studio mcp install --client claude-code   # or: cursor · claude-desktop · windsurf · lovable
# manual: add {"command": "husk-studio", "args": ["mcp"]} under the client's mcpServers
```

Replay executes your agent code and is off by default — enable it locally with
`husk-studio mcp --enable-replay` (or `HUSK_MCP_ENABLE_REPLAY=1`). For remote clients
(Lovable), use `husk-studio mcp --transport http` behind a tunnel.

See the [project README](https://github.com/husk-ai-lab/husk-ai) for full
docs, IDE integrations (Cursor, VS Code, Antigravity), and OTel instrumentation
patterns Husk understands.

## License

Source-available under the Business Source License 1.1 (BUSL 1.1). See the
[`LICENSE`](https://github.com/husk-ai-lab/husk-ai/blob/main/LICENSE) at the
repository root.
