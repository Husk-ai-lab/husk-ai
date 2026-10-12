# husk-cursor-hook

Cursor bridge for [Husk](https://github.com/husk-ai-lab/husk-ai) — the local-first,
development-time debugger for the agent you're building.

Streams Cursor's `afterFileEdit` and `stop` events into the Husk backend running
on **your own machine**, so the Studio timeline shows every file the agent touched
while you develop it, alongside the LLM and tool calls captured via OpenTelemetry.

Everything stays local: Husk's trace ingest is loopback-only, so this bridge only
reaches a Husk running on the same machine. It can't stream to a remote host, and
Husk can't be pointed at a production deployment elsewhere — it's a development tool
by design.

The bridge is fire-and-forget. It never blocks the Cursor agent and never returns
a decision.

## Install

```bash
npm install -g husk-cursor-hook
```

## Use

In your Cursor project, run:

```bash
husk-cursor-hook install
```

This writes `.cursor/hooks.json` registering the `afterFileEdit` and `stop`
events with Cursor.

Then start Husk:

```bash
# Install from source (PyPI release is on the roadmap)
git clone https://github.com/husk-ai-lab/husk-ai.git && cd husk-ai
uv sync --all-packages
uv run husk-studio start
```

Open `http://localhost:7654` to see the Studio.

## How it works

Each registered Cursor hook event runs `husk-cursor-hook hook --event=<name>`.
The script POSTs the payload to `http://localhost:7654/api/cursor/events` as
fire-and-forget, then writes an empty JSON response to stdout so Cursor
proceeds immediately.

If Husk isn't running, the bridge logs to stderr and exits cleanly — your
Cursor session is never affected.

## Environment

- `HUSK_URL` — override the Husk backend URL (default `http://localhost:7654`).
