# AGENTS.md — set up husk-studio, end to end

**Read this first if you are an AI assistant.** This file is a runbook. Follow it top to bottom
and the person you're helping ends with husk-studio running, a real run on screen, and their own
agent streaming into it.

It is written so it works **even if you cannot run commands yourself**. You give the command,
the user runs it, they paste the output back, you check it against the expected result printed
right there in this file. You never have to guess whether a step worked.

Repository: <https://github.com/husk-ai-lab/husk-ai>

---

## 0. How to use this file

**If you cannot run shell commands** (you're a chat assistant, or no terminal tool is
available) — you are the driver, the user is the hands:

1. Give **exactly one command at a time.** Never dump a block of five and hope.
2. Say what success looks like **before** they run it.
3. Ask them to paste the output back.
4. Compare it against the *Looks right* block for that step. Only then move on.
5. If it doesn't match, go to [§11 When it breaks](#11-when-it-breaks) and match the literal
   error text. Do not improvise a fix.

**If you can run shell commands** — run them yourself, but still stop at every **Checkpoint**
and confirm before continuing. The checkpoints are exactly where setup goes wrong.

**Before command one**, settle two things and don't ask again:

- **Which OS?** Windows means PowerShell (not the old Command Prompt). macOS and Linux mean
  bash or zsh. Commands below are marked when they differ. Everything unmarked is identical.
- **Docker or source?** If they just want to look at husk-studio and don't intend to change it,
  jump to [§10 Docker](#10-docker-the-no-clone-path) — it's one command. Everything else in
  this file assumes the source clone, which is what you want if they'll connect their own
  agent from the same machine.

Time budget: about 10 minutes, most of it waiting on one download.

---

## 1. What husk-studio is, in four lines

Describe it this way. Getting this wrong sends the user down the wrong path.

- A **visual debugger for an AI agent you are building** — not an observability platform, not
  an APM, not a monitoring service.
- **Local-first.** Everything stays on the user's machine. There is no account, no login, no
  cloud, no telemetry back to anyone.
- **Development-time only, enforced in code.** Trace ingest only accepts loopback callers, so
  only an agent running on *that same machine* can stream in. It cannot be pointed at a staging
  or production deployment. If the user asks for that, the answer is no — by design, not by
  configuration.
- **One outbound call, optional.** Only the automatic debugger reaches out, to a model provider
  the user picks and brings their own key for. Skip that feature and nothing leaves the box.

---

## 2. Prerequisites

Three checks. Run them one at a time — a user with a broken PATH will fail all three and it's
much easier to diagnose one at a time.

| Command | Needed | Looks right |
|---|---|---|
| `git --version` | yes | `git version 2.39.0` (any 2.x) |
| `uv --version` | yes | `uv 0.5.11` (any 0.4 or newer) |
| `node --version` | effectively yes — see below | `v20.11.0` (any v20 or newer) |

**About Node.** The docs call it optional because the API and CLI run without it. Be honest
with the user: without Node the Studio bundle can't build and they get a placeholder page
instead of the interface. Since the interface *is* the product, treat Node as required unless
they only want the CLI and MCP server.

### Installing what's missing

**uv** — Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**uv** — macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

> **The single most common failure in this whole setup.** After installing uv (or git, or
> Node), the *current* terminal does not know about it yet. The user must **close the terminal
> completely and open a new one**, then re-run `uv --version`. If you skip this, the next three
> steps all fail with "not recognized" / "command not found" and it looks like the install
> broke. It didn't.

**git**: <https://git-scm.com/downloads> · **Node 20+**: <https://nodejs.org> — same
close-and-reopen rule for both.

The user does **not** install Python. uv reads `.python-version` in the repo and fetches Python
3.11 itself. Any other Python on the machine is ignored. Don't let them install one.

---

## 3. Clone and sync

```bash
git clone https://github.com/husk-ai-lab/husk-ai.git
```

```bash
cd husk-ai
```

```bash
uv sync --all-packages
```

**This takes 1 to 3 minutes and downloads roughly 700 MB** the first time — it fetches Python
3.11 plus every dependency. It is not frozen. Tell the user that before they run it, or they
will Ctrl+C a working install. After this, husk-studio runs fully offline.

**Looks right** — a resolved/installed summary ending with something like:

```
Resolved 180 packages in 2.31s
Installed 174 packages in 41.20s
```

Everything from here runs from inside the `husk-ai` folder.

---

## 4. Checkpoint A — does it work at all?

```bash
uv run husk-studio doctor
```

**Looks right** — five lines, then a next-steps block:

```
husk-studio: 0.8.0
home: C:\Users\you\.husk
db:   C:\Users\you\.husk\traces.db  missing (created on first `husk-studio start`)
mcp:  ready  connect with: husk-studio mcp
      replay: disabled (read-only) (--enable-replay or HUSK_MCP_ENABLE_REPLAY=1; local-only)

Next steps:
  husk-studio start        backend + Studio
  husk-studio demo         seed a sample run
  husk-studio run <cmd>    capture your own agent
```

How to read it:

- `db: … missing` at this point is **correct**, not an error. The database is created on first
  `start`. It should say `ok` after §5.
- `mcp: ready` means the MCP server can run. `'mcp' package missing` means the sync didn't
  finish — re-run `uv sync --all-packages`.
- `replay: disabled (read-only)` is the **safe default** and should stay that way unless the
  user explicitly wants [§9 MCP replay](#9-optional-mcp--let-a-coding-assistant-read-the-runs).

If this command runs, the install is sound. If it doesn't, nothing after this will work — fix
it here using [§11](#11-when-it-breaks).

---

## 5. Start it

```bash
uv run husk-studio start
```

**Looks right** — a log line with the URL, then uvicorn holding the terminal open:

```
husk-studio starting on http://127.0.0.1:7654
INFO:     Uvicorn running on http://127.0.0.1:7654 (Press CTRL+C to quit)
```

The browser opens by itself at <http://localhost:7654> after about a second. First boot also
builds the Studio bundle — that adds 10 to 30 seconds once, then never again.

Three things to tell the user, in this order:

1. **Leave this terminal running.** Closing it stops husk-studio. `Ctrl + C` to stop on purpose.
   Everything from here needs a **second terminal**, `cd`'d into the same `husk-ai` folder.
2. They land on **Welcome to husk-studio**. No account, no login, no sign-up — it is single-user
   and runs only for them, on that machine. Click through to the empty run list.
3. **Read the port off that log line, don't assume 7654.** If 7654 was busy, husk-studio silently
   takes the next free port between 7655 and 7664 and writes the real one to `~/.husk/port`.
   The URL in the log is the truth. To pin it: `uv run husk-studio start --port 7656`.

> **Got a placeholder page saying the Studio isn't built yet?** Node is missing or the build
> failed. See the Studio row in [§11](#11-when-it-breaks). The backend is fine — only the UI
> didn't build.

---

## 6. Checkpoint B — put a run on screen

Second terminal, in the `husk-ai` folder:

```bash
uv run husk-studio demo
```

This writes one IDE event and a 3-span OpenTelemetry trace with GenAI attributes. Refresh the
Studio: a run appears under **Runs** within about two seconds.

**If the run appears, the whole pipeline works** — CLI, backend, ingest, database, and UI. Say
so. That is the confidence checkpoint.

If the command succeeds but nothing shows up, the Studio is probably pointed at a different
port than the backend bound. Re-check the log line from §5.

---

## 7. Checkpoint C — the actual product

`demo` only proves the plumbing. It emits a flat trace with no graph and no state, so the
feature husk-studio exists for — edit a step and re-run from there — isn't visible yet. Do this
one. It takes fifteen seconds and it's the thing worth showing.

```bash
uv run --group examples python examples/husk_thread.py
```

This runs a real 2-node graph (`planner` → `answerer`) about "Rome" on husk-studio's own engine.

**Looks right:**

```
Thread:  3c81f1f6-a3d8-4475-be95-ef14048bc889
State:   {'topic': 'Rome', 'plan': '1. Research Rome\n2. Summarize\n3. Format final answer', 'answer': 'Rome is the capital of Italy with ~2.87M people.'}
Open http://127.0.0.1:7654/runs to see the run, then 'Modify and replay'.
```

Now walk them through the replay, in the Studio:

1. Open the new run. Unlike the demo run, this one has a **node graph** and a per-node **state
   diff**.
2. Click **Modify and replay**. A JSON editor opens with the run's state.
3. Change `"topic": "Rome"` to `"topic": "Tokyo"`.
4. Click **Run from here** on the **`answerer`** node — not `planner`.

**Looks right:** a new run appears whose answer is
`Tokyo is the capital of Japan with ~14M people.`

**Then point at the `plan` field**, because it is the proof and it's easy to miss: it still
reads `1. Research Rome`. `planner` never re-ran. husk-studio resumed from the state snapshot taken
*before* `answerer`, so only `answerer` executed — the upstream node emitted no spans, spent no
tokens, and cost nothing. The original run is untouched; the new one is recorded as a branch
off it. On a real agent that's the difference between re-running a 40-step pipeline and
re-running the one step that broke.

> **Modify and replay greyed out?** Expected on any run that arrived as pure OpenTelemetry
> telemetry. husk-studio can only re-execute a run when it can reach the code that produced it. See
> the two tiers in [docs/replay.md](docs/replay.md).

---

## 8. Connect the user's own agent

This is the one and only code change in their project. Pick **one** path — don't present all
three and make them choose.

**Decision rule:**

- Python, using the OpenAI / Anthropic / LangChain / LangGraph / LlamaIndex SDK → **Path A**.
- They'd rather not import husk-studio at all, or they're not on Python → **Path B**.
- They already own a `TracerProvider` and want explicit control → **Path C**.

Whichever path, the agent must run **on the same machine** as husk-studio. Ingest refuses
non-loopback callers. There is no configuration that changes this.

### Path A — one line

```bash
uv pip install 'husk-studio-shared[openai]'
```

Swap the extra for `anthropic`, `langgraph`, or `llamaindex` as needed.

```python
from husk_studio_shared import instrument_openai

instrument_openai()   # every OpenAI SDK call now streams into husk-studio
# ...then use the SDK exactly as before. Nothing else changes.
```

### Path B — an environment variable, zero code

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:7654 python your_agent.py
```

PowerShell:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:7654"; python your_agent.py
```

Works with any OpenTelemetry SDK in any language that follows the GenAI semantic conventions.
husk-studio appends `/v1/traces` itself.

### Path C — wire the exporter yourself

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
```

### Or skip the wiring entirely

One command starts the backend if it isn't up, points the exporter at it, runs the agent, and
prints the run URL:

```bash
uv run husk-studio run python my_agent.py
```

**Then verify.** Have them run their agent once and confirm the run lands under **Runs** with
the fields they expect. husk-studio records exactly what the instrumentor emits — no more. If a
prompt or token count is missing, the gap is upstream in the instrumentor, not in husk-studio. See
[docs/instrumentation.md](docs/instrumentation.md#a-note-on-capture).

---

## 9. Optional — the extras

Everything up to here works with no API key at all. These two are opt-in.

### The automatic debugger (needs a key)

When a run fails, one click makes husk-studio read the whole run and explain, in plain language,
what broke and what to change. It needs a model provider key.

**Hard rule: never ask the user to paste an API key into this chat.** Send them to the Studio's
**Settings** page to enter it there. It is stored in `~/.husk/secrets.json` on their machine
and is never logged, never returned to the UI, and never sent to any husk-studio server.

| Provider | Default? | Key via environment variable |
|---|---|---|
| **Regolo.ai** | yes — EU-hosted, GDPR, zero retention | `REGOLO_API_KEY` |
| Anthropic | | `ANTHROPIC_API_KEY` |
| OpenAI | | `OPENAI_API_KEY` |
| OpenRouter | | **none — Settings only** |

If they already have one of the first three exported in their shell, husk-studio picks it up and
they can skip Settings. OpenRouter has no environment-variable fallback; its key must go
through Settings.

This is the only feature that sends anything off the machine. Skip it and husk-studio is fully
offline. Details: [docs/ai-layer.md](docs/ai-layer.md).

### MCP — let a coding assistant read the runs

Connects husk-studio's local trace database to Claude Code, Cursor, Claude Desktop, or Windsurf, so
an assistant can answer "which of my last runs failed, and why?" directly.

```bash
uv run husk-studio mcp install --client claude-code
```

Also accepts `cursor`, `claude-desktop`, `windsurf` (each writes that client's config file) and
`lovable` (prints tunnel instructions instead — it's a remote client).

The read tools are `list_runs`, `get_run`, `get_trace`, `get_span`, `list_errors`,
`cost_breakdown`, `dashboard_summary`, and `list_cursor_events`. They read
`~/.husk/traces.db` directly, so they work even when the backend isn't running.

`replay_run` is **not** exposed by default. It executes the user's code. Only enable it if they
ask, with `husk-studio mcp --enable-replay`.

Connecting the server isn't enough — the assistant also has to know to reach for it.
[docs/mcp.md](docs/mcp.md#make-your-assistant-actually-use-them) has a ready-to-paste rules
block for exactly that.

---

## 10. Docker — the no-clone path

For a user who wants to look at husk-studio without cloning or installing uv. They get the Studio
and the CLI; they do **not** get the source, so they can't run the bundled examples against
their own edits.

```bash
docker run -d --name husk -p 127.0.0.1:7654:7654 -v husk-data:/data ghcr.io/husk-ai-lab/husk-ai
```

Then <http://localhost:7654>. Seed a run with:

```bash
docker exec husk husk-studio demo
```

`docker compose up -d` works too — the compose file is in the repo root.

**The catch worth stating up front:** ingest is loopback-only *relative to the container*. An
agent running on the host cannot stream into a containerised husk-studio. If the user's whole point
is connecting their own agent, use the source path instead. Full details:
[docs/docker.md](docs/docker.md).

---

## 11. When it breaks

Match on the **literal text** the user pastes. Don't guess.

### Before it starts

| What they pasted | Why | Fix |
|---|---|---|
| `uv : The term 'uv' is not recognized…` | PowerShell hasn't reloaded PATH | Close PowerShell **completely**, open a new window, `uv --version`. Per-session escape: `$env:Path = "$HOME\.local\bin;" + $env:Path` |
| `command not found: uv` | same, macOS/Linux | New shell, or `source ~/.zshrc` / `source ~/.bashrc` |
| `git : The term 'git' is not recognized` | same cause after the git installer | Close and reopen the terminal |
| PowerShell refuses to run the install script | ExecutionPolicy | Use the install line **exactly** as written in §2 — its `-ExecutionPolicy ByPass` scopes to that one call. Do not change the system policy |
| `uv sync` → `Failed to fetch …` | network or corporate proxy | Set `HTTPS_PROXY` and retry |
| `uv sync` fine, but `husk-studio` "command not found" | sync ran outside the repo, or partially | `cd` into the repo root, re-run `uv sync --all-packages` |
| `ImportError: cannot import name 'StrEnum' from 'enum'` | venv predates the Python 3.11 pin | Delete `.venv` (`rm -rf .venv`, or `rmdir /s .venv` on Windows) and re-sync |

### Running, but wrong

| What they see | Why | Fix |
|---|---|---|
| Browser: can't connect to localhost:7654 | port was taken, husk-studio bound elsewhere | Read the real URL off the `husk-studio starting on…` log line, or `cat ~/.husk/port`. Pin it with `--port 7656` |
| A page saying the Studio isn't built yet | Node missing, so the bundle couldn't auto-build | Install Node 20+ and restart. Or build by hand: `corepack pnpm install` then `corepack pnpm --filter studio build`, then reload |
| Studio loads but can't reach the backend | dev server proxying to the wrong port | Check `VITE_BACKEND_PORT` in `apps/studio/.env.local` |
| **Modify and replay** greyed out | that run has no graph module — expected for OTel-only runs | Not a bug. See [docs/replay.md](docs/replay.md) |

### Agent not showing up

| What they see | Why | Fix |
|---|---|---|
| Agent runs, nothing reaches husk-studio | agent isn't on this machine, or endpoint is wrong | Ingest is loopback-only **by design**. Point the exporter at `http://localhost:7654` and run the agent on the same machine. There is no remote-ingest option |
| `AuthenticationError: Incorrect API key` | **their agent's** key, not husk-studio's | `$env:OPENAI_API_KEY = "sk-..."` (PowerShell) or `export OPENAI_API_KEY=sk-...` (bash). Never commit it |
| `ModuleNotFoundError: No module named 'opentelemetry'` | OTel SDK missing in their project | `pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http` |
| Run appears, timeline empty | no root span | Wrap the top-level loop: `with tracer.start_as_current_span("agent.run"):` |
| Run appears, prompts and completions missing | no `gen_ai.*` events | Add `span.add_event("gen_ai.user.message", {"content": prompt})` and `span.add_event("gen_ai.choice", {"message.content": completion})` |

**Still stuck?** These two produce everything needed for a bug report:

```bash
uv run husk-studio doctor
```

```bash
uv run husk-studio export <run_id>
```

The export bundle is already secret-redacted, so it's safe to attach. Issues:
<https://github.com/husk-ai-lab/husk-ai/issues>. More symptoms:
[docs/troubleshooting.md](docs/troubleshooting.md).

---

## 12. Reference card

### Commands

| Command | What it does |
|---|---|
| `husk-studio start` | Boot the backend and Studio. `--port`, `--no-open-browser` |
| `husk-studio run <cmd…>` | Start the backend if needed, point the agent at it, run it, print the run URL |
| `husk-studio demo` | Seed a sample trace |
| `husk-studio list` | Recent runs in the terminal |
| `husk-studio replay <run_id>` | Replay from the terminal. `--set k=v`, `--span <id>`, `--cassette` |
| `husk-studio export <run_id>` | Portable, secret-redacted JSON bundle. `--out FILE` |
| `husk-studio doctor` | Version, paths, health check. Run this first when something's off |
| `husk-studio delete <run_id>` | Drop one run. Prompts first; `--yes` skips. Prefer this over `clean` |
| `husk-studio clean` | **Irreversible.** Wipes `~/.husk/`. Never run this for the user unasked |
| `husk-studio mcp` | Run the MCP server. `--transport http` (port 7655), `--enable-replay` |
| `husk-studio mcp install --client <name>` | Write a client's MCP config |

Prefix with `uv run` inside a source clone. Full reference: [docs/cli.md](docs/cli.md).

### Ports

| Port | What |
|---|---|
| **7654** | Backend, Studio, and OTLP trace ingest. Auto-bumps to the next free port up to 7664 |
| 7655 | MCP server, only with `--transport http` or `sse` |
| 5174 | Vite dev server, only when developing the Studio UI |

### Paths — everything husk-studio writes

| Path | What |
|---|---|
| `~/.husk/traces.db` | Runs and spans: prompts, completions, tool I/O, cost. **Cleartext by design** |
| `~/.husk/runs/<run_id>/` | Per-run inputs, outputs, snapshots, cassettes |
| `~/.husk/cassettes/` | Recorded provider HTTP responses for model-free replay |
| `~/.husk/secrets.json` | The debugger's provider key. `0600` on POSIX |
| `~/.husk/port` | The port the running backend actually bound |
| `<repo>/.venv/` | Python dependencies |

To wipe everything: `uv run husk-studio clean`. To uninstall: delete the repo folder and `~/.husk/`.

### Environment variables

There is **no `.env` file and no `.env.example`** — husk-studio reads the environment directly.
Don't invent one.

| Variable | Effect |
|---|---|
| `HUSK_HOME` | Move the data directory off `~/.husk` |
| `HUSK_LOG` | CLI log level: `debug` `info` `warning` `error` |
| `HUSK_NO_BROWSER` | Don't open a browser on `start` |
| `HUSK_NO_REDACT` | Disable secret-shape redaction on ingest. Think before suggesting this |
| `HUSK_ALLOWED_GRAPH_DIRS` | Extra directories replay may import graph modules from |
| `HUSK_MCP_ENABLE_REPLAY` | Expose the code-executing `replay_run` MCP tool. Off by default |
| `HUSK_REPLAY_CASSETTE` | Serve replay LLM calls from the recorded cassette |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where **their agent** sends traces |

---

## 13. Rules for you, the assistant

- **Never fabricate output.** If you can't run a command, say so and ask the user to paste the
  result. A made-up success message is worse than no message.
- **Never ask for an API key in chat.** Settings page, or their own shell environment. Nowhere
  else.
- **Never run `husk-studio clean` on your own initiative.** It irreversibly wipes every recorded
  run.
- **Don't enable `--enable-replay` casually.** It executes the user's code from an MCP tool.
- **Don't offer to point husk-studio at staging or production.** Loopback-only ingest is enforced
  in code and is a deliberate product boundary, not a limitation to work around.
- **One command at a time, and wait.** Every checkpoint in this file exists because that's
  where setups fail.
- **Ground claims in what you actually read** — a real `run_id`, a real span, a real pasted
  error. Don't infer what a run contained.

---

## 14. If you're here to change the code

Different job from the one above. Short version:

```bash
uv sync --all-packages --group examples
```

Before opening a PR, all four must pass:

```bash
uv run ruff check .
```

```bash
uv run mypy
```

```bash
uv run python -m pytest -q
```

```bash
uv run python benchmark/reproduce.py
```

Touched the Studio UI? Also `corepack pnpm --filter studio check` and
`corepack pnpm --filter studio build`.

Layout: `packages/` holds the four Python workspace packages (`husk-studio-cli` ships the `husk-studio`
command, `husk-studio-shared` the schemas and replay engine, `husk-studio-backend` the FastAPI app,
`husk-studio-sandbox` the HTTP cassettes). `apps/studio/` is the React Studio. `packages-npm/` holds
the Cursor and VS Code bridges. Architecture: [docs/architecture.md](docs/architecture.md).

Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`). Contribution
terms and the BUSL-1.1 licence: [CONTRIBUTING.md](CONTRIBUTING.md).
