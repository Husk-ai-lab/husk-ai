# Troubleshooting

Find your symptom, apply the fix, get back to debugging your agent. Problems fall into three buckets:

- **Setup** errors happen before Husk starts.
- **Runtime** errors happen when the backend is up but something is off.
- **Agent connection** errors mean your agent is not streaming traces in.

If you only have one minute, run the doctor first. It prints the installed version, your `~/.husk/` home, the database path, and a health check:

```bash
uv run husk-studio doctor
```

For setup steps and connecting an agent, see the [Getting started](./getting-started.md) and [Instrumentation](./instrumentation.md) pages. Everything below is local and single-user: Husk runs on your machine, keeps zero data retention, and accepts traces only over loopback — so only an agent running on this same machine can stream in. The only outbound calls come from the automatic debugger reaching the model provider you configure.

## Setup errors

These happen before Husk boots. Most are PATH problems after a fresh install: the tool installed correctly, but your current shell has not reloaded its PATH.

| Symptom | Fix |
| --- | --- |
| `uv : The term 'uv' is not recognized…` (PowerShell) | `uv` installed in this session but the current PowerShell has not reloaded its PATH. Close PowerShell completely, open a fresh window, verify with `uv --version`. |
| `command not found: uv` (macOS / Linux) | Same cause. Open a new shell, or `source ~/.bashrc` / `source ~/.zshrc` to reload PATH in the current one. |
| `git : The term 'git' is not recognized` (PowerShell) | Same root cause. Close and reopen the terminal after the git installer finishes, then verify with `git --version`. |
| Install ran, `~/.local/bin/uv.exe` exists, still "not recognized" | Per-session fix in PowerShell: `$env:Path = "$HOME\.local\bin;" + $env:Path`. Permanent fix: open a new terminal. |
| PowerShell cannot run script (ExecutionPolicy) | Use the install command exactly as written. It includes `-ExecutionPolicy ByPass`, which scopes the exception to that single invocation. Do not change your system policy. |
| `uv sync` fails with "Failed to fetch …" | Network or proxy issue. Behind a corporate proxy, set `HTTPS_PROXY` and retry. |
| `uv sync` succeeds but `uv run husk-studio start` says "command not found" | Re-run `uv sync --all-packages` from the repo root. The `husk-studio` script ships with the `husk-studio-cli` workspace package. |
| `ImportError: cannot import name 'StrEnum' from 'enum'` | Your venv predates the Python 3.11 pin. Delete and resync: `rmdir /s .venv` (PowerShell) or `rm -rf .venv` (mac/Linux), then `uv sync --all-packages`. |

## Runtime errors

The backend is up, but the Studio will not load or behaves oddly.

| Symptom | Fix |
| --- | --- |
| Browser shows "can't connect to localhost:7654" | Backend crashed before binding. Check the terminal output. Most common cause: port 7654 is in use. Run `husk-studio start --port 7656` instead. |
| Backend served a fallback "Studio isn't built yet" page | The Studio bundle is missing and auto-build was skipped, most likely because Node.js is not installed. Install Node 20+, then restart `uv run husk-studio start`. Or build manually: `corepack pnpm install` then `corepack pnpm --filter studio build` from the repo root, then reload the page. |
| Studio loads but cannot reach the backend (blank page, console connection errors) | The Studio dev server proxies to a different port. Check `VITE_BACKEND_PORT` in `apps/studio/.env.local`. |

## Agent connection errors

The backend is healthy, but your agent is not streaming traces in. Husk accepts traces only over loopback, so the agent must run on this same machine.

| Symptom | Fix |
| --- | --- |
| Agent runs but nothing reaches Husk | Trace ingest is loopback-only by design. Point the exporter at `http://localhost:7654` and run the agent on this machine. Husk refuses ingest from another host, so it cannot be pointed at a remote or production deployment. |
| `AuthenticationError: Incorrect API key` (your agent) | Your OpenAI or Anthropic key is not in the terminal session. Set it: `$env:OPENAI_API_KEY = "sk-..."` (PowerShell) or `export OPENAI_API_KEY=sk-...` (bash). Never commit it. |
| `ModuleNotFoundError: No module named 'opentelemetry'` (your agent) | Install the OTel SDK and exporter: `pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`. |
| Run appears in /runs but the timeline is empty | The agent never opened a root span. Wrap your top-level loop: `with tracer.start_as_current_span("agent.run"):`. |
| Run appears but prompts / completions are missing | The agent emitted no `gen_ai.*` events. On each LLM span, add `span.add_event("gen_ai.user.message", {"content": prompt})` and `span.add_event("gen_ai.choice", {"message.content": completion})`. |
| "Modify and replay" button is disabled | The root span lacks the `husk.graph_module` attribute. Set `root.set_attribute("husk.graph_module", f"{__file__}:graph")`. Only runs that expose a graph module support [replay](./replay.md). |

## Where files live

Every directory Husk touches on your machine.

| Asset | Path |
| --- | --- |
| uv binary (Windows) | `%USERPROFILE%\.local\bin\uv.exe` |
| uv binary (mac/Linux) | `~/.local/bin/uv` |
| uv cache of downloaded packages | `%LOCALAPPDATA%\uv\cache\` (Windows) / `~/.cache/uv/` (mac/Linux) |
| Python interpreters uv installs | `%APPDATA%\uv\python\` (Windows) / `~/.local/share/uv/python/` (mac/Linux) |
| Project dependencies for Husk | `<repo>/.venv/` (inside the husk-ai folder) |
| Studio source code (what you edit) | `<repo>/apps/studio/client/` |
| Studio built bundle (what the backend serves) | `<repo>/apps/studio/dist/` |
| Husk runtime data (your runs and traces) | `~/.husk/` (SQLite DB, `runs/`) |

To uninstall everything Husk-related, delete the `husk-ai` repo folder (this removes `.venv/`) and `~/.husk/`. To uninstall uv, delete `~/.local/bin/uv*` and the cache folder. Nothing touches your Windows registry or system Python.

## Still stuck?

Two fast diagnostics before you file anything:

```bash
uv run husk-studio doctor              # version, home, DB path, health check
uv run husk-studio export <run_id>     # portable, secret-redacted run bundle
```

The `export` bundle (run, spans, and branches, already secret-redacted) is the single best attachment for a bug report: it captures the exact trajectory without leaking keys. If your symptom is not above, open an issue on [GitHub](https://github.com/husk-ai-lab/husk-ai/issues). For the full command set, see the [CLI reference](./cli.md), and start from the [README](../README.md) for the big picture.
