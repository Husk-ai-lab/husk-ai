# CLI reference

The `husk-studio` CLI is how you boot Husk, capture an agent run, and drive the debugging loop
from the terminal while you build your agent — before it goes to production. Run any command
with `uv run husk-studio <command>` in the cloned repo. Everything stays on your own machine:
trace ingest is loopback-only, so only an agent running here can stream in. For the product
overview, see the [README](../README.md).

## Commands

| Command | What it does |
| --- | --- |
| `husk-studio start` | Boot the server (default port 7654) and open the Studio in your browser. Auto-builds the Studio bundle on first run if missing. Override the port with `--port 7656`. Skip the browser open with `--no-open-browser`. |
| `husk-studio run <command…>` | Run your agent and capture it in one step. Ensures the backend is up (auto-starts it if needed), points your agent's OpenTelemetry exporter at Husk via `$OTEL_EXPORTER_OTLP_ENDPOINT`, runs the command, and prints the run URL. Use `--no-serve` to exit when the command finishes (for CI). |
| `husk-studio demo` | Seed one IDE observability event plus a 3-span OTel trace with GenAI attributes. Verify the Studio renders integration tiles and traces correctly before connecting your own agent. |
| `husk-studio list` | List recent runs in the terminal (run id, framework, span count, cost). |
| `husk-studio replay <run_id>` | Re-run a recorded run with modified state from the terminal or CI. `--set key=value` overrides state (value parsed as JSON, else string), `--span <id>` forks from a node, `--cassette` serves the LLM from the recorded HTTP cassette ($0, deterministic). |
| `husk-studio export <run_id>` | Export a run (run, spans, and branches, already secret-redacted) to a portable JSON bundle. Writes to `--out FILE`, else stdout. Good for bug reports and sharing a trajectory. |
| `husk-studio doctor` | Diagnostics: prints the installed version, your `~/.husk/` home, the DB path, and a health check. Run this first when something feels off. |
| `husk-studio delete <run_id>` | Delete one run and its spans, snapshots, branches, and cassettes. Prompts first; `--yes` skips. Replays forked from it are kept and just lose their parent pointer. |
| `husk-studio clean` | Wipe the local database and runs directory at `~/.husk/`. Removes all recorded runs and traces. Does not delete the cloned repo or your `.venv`. |
| `husk-studio mcp` | Run the MCP server so AI coding tools (Claude Code, Cursor, Windsurf, Lovable) can read your runs and traces, analyze cost, and replay. stdio by default. `--transport http` for remote clients. `--enable-replay` exposes the (local-only) replay tool. |
| `husk-studio mcp install --client <name>` | Write or print the MCP config to connect a client. One of `claude-code`, `cursor`, `claude-desktop`, `windsurf`, `lovable`. |

The legacy `husk` alias still works inside this workspace, so older scripts do not
break.

## Common flows

Boot the Studio and open it in your browser:

```bash
uv run husk-studio start
```

Capture an agent run and print its URL, no manual exporter setup:

```bash
uv run husk-studio run python my_agent.py
```

Reproduce a failure offline, deterministic and at $0, by replaying from the recorded
cassette:

```bash
uv run husk-studio replay <run_id> --cassette --set retries=3
```

Verify the Studio renders correctly before wiring in your own agent:

```bash
uv run husk-studio demo
```

When something feels off, start here:

```bash
uv run husk-studio doctor
```

## Related

- [replay](./replay.md): the modify-and-replay engine behind `husk-studio replay`.
- [mcp](./mcp.md): connect Claude Code, Cursor, and other clients to your runs.
- [README](../README.md): what Husk is and how the pieces fit together.
