# Husk for VS Code

The Husk bridge for **VS Code**, **Antigravity** (Google's VS Code fork),
and any other VS Code-compatible IDE.

Streams every terminal command your AI agent runs — including commands
fired by Copilot, Continue, Cline, Roo, or Antigravity's native agent —
into your local Husk at `http://localhost:7654`, so you can see what your
agent actually did, with arguments and cwd, while you build it. The
commands land on the same Studio timeline as the LLM and tool calls
captured via OpenTelemetry.

Husk is a local-first, single-user, development-time debugger: you run it
on your own machine while building an agent, before production. The
backend only accepts traces over loopback, so only an agent running on
this machine can stream in — it cannot be pointed at a deployment on
another host.

## Install

1. Have Husk running locally:
   ```bash
   git clone https://github.com/husk-ai-lab/husk-ai.git && cd husk-ai
   uv sync --all-packages
   uv run husk-studio start
   ```
2. Install this extension in your IDE (VS Code, Cursor, Antigravity, …):
   ```bash
   code --install-extension husk-vscode-hook
   ```
   (Or open the marketplace, search "Husk".)
3. Open any terminal in the IDE. The status bar shows `● Husk` once the
   extension can see your local backend.

## What you get today

| Capability | Status |
|---|---|
| Stream every terminal command into your local Husk | ✅ |
| Tag events by IDE (vscode / cursor / antigravity) | ✅ |
| Group commands by run / agent thread | ⏳ roadmap |

This bridge only records — it streams what your IDE agent does onto the
Studio timeline so you can debug the run. It never blocks the IDE or
prompts you for a decision.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `husk.url` | `http://localhost:7654` | Where to send events. Override if you ran `husk-studio start --port <other>`. |
| `husk.captureTerminal` | `true` | Toggle terminal capture. Use `Husk: Toggle Terminal Capture` from the command palette. |

## Commands

- `Husk: Open Studio` — opens the Husk Studio in your browser
- `Husk: Reconnect` — re-pings the backend if you restarted it
- `Husk: Toggle Terminal Capture` — turn capture on/off temporarily

## Privacy

The extension only talks to the URL you set in `husk.url`, which
defaults to your own `localhost`. Commands and cwd never leave your
machine, and the Husk backend only accepts traces over loopback — there
is no remote ingest to send them to.

## License

Source-available under the Business Source License 1.1 (BUSL 1.1). See the
[`LICENSE`](https://github.com/husk-ai-lab/husk-ai/blob/main/LICENSE) at the
repository root.
