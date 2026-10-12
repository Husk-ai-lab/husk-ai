# Frontend ↔ Backend Connection Architecture (this repo)

> **Scope:** how `apps/studio` (the React UI) actually talks to the Python backend at runtime —
> process boot, port discovery, transport, and the REST/WebSocket calls themselves. For the
> broader system (OTel ingest, SQLite, the replay engine, the debugger's outbound call), see
> [architecture.md](architecture.md). For comparison against a desktop-wrapped design, see
> [reference-hermes-electron-python-architecture.md](reference-hermes-electron-python-architecture.md).

## The one-line shape

There is no separate frontend server, no IPC layer, and no desktop shell. One Python process
serves both the API and the built UI on one port, and the OS's own browser is the direct client
of that API — the same trust model as any local web app.

```
uv run husk-studio start
      │
      ▼
husk.cli:main()  (Click group, entry point from packages/husk-studio-cli/pyproject.toml)
      │
      ▼
start()  →  server.start_server()          packages/husk-studio-cli/src/husk/server.py
      │        ├─ _resolve_port(7654)      picks 7654, or scans 7654..7664 if busy
      │        ├─ _write_port_file()       ~/.husk/port  (so `demo`/`replay` can find it)
      │        ├─ threading.Timer(1.2s) → webbrowser.open(url)   ui_launcher.py
      │        └─ uvicorn.Server(Config("husk_studio_backend.main:app", ...)).run()
      ▼                                                                    (blocks, in-process)
FastAPI app  (packages/husk-studio-backend/src/husk_studio_backend/main.py)
      │
      ├─ GET /                → FileResponse(dist/index.html)        SPA shell
      ├─ GET /assets/*         → StaticFiles mount                    built JS/CSS
      ├─ GET /{full_path}      → index.html fallback                  client-side routing
      ├─ GET /api/v1/*         → REST (runs, spans, graph, diff, ...)
      ├─ GET /api/dashboard/*  → REST (dev-home summary)
      ├─ GET|PUT /api/debugger/* → REST (BYOK config, analyze, apply-fix)
      ├─ POST /api/replay      → REST (modify-and-replay)
      ├─ WS   /ws/runs/{id}    → live span/run events
      └─ POST /v1/traces       → OTLP ingest (loopback-only; the agent's telemetry lands here)
```

One process, one port, no build step at request time (the SPA is prebuilt — see below).

## 1. Process boot: no subprocess for the UI

Unlike an architecture where a shell process spawns a separate backend process it then has to
health-check (the Hermes/Electron pattern), here the CLI *is* the backend process. `start()` in
[cli.py:50-54](../packages/husk-studio-cli/src/husk/cli.py:50) calls `start_server()` in
[server.py:48-73](../packages/husk-studio-cli/src/husk/server.py:48), which constructs a
`uvicorn.Config` for `husk_studio_backend.main:app` and calls `.run()` **in the same process** —
no `subprocess.Popen`, no readiness polling, no second interpreter to wait on. There is nothing
analogous to Hermes's `waitForHermesReady()` handshake because there is no second process to
wait for.

## 2. Port discovery: resolved before bind, not announced after

`_resolve_port()` ([server.py:23-33](../packages/husk-studio-cli/src/husk/server.py:23)) tries 7654,
then scans 7654–7664 for the first free port, entirely on the Python side, *before* uvicorn
binds. The chosen port is written to `~/.husk/port` so other CLI commands (`demo`, `replay`) can
find the running backend, and it's baked into the `http://localhost:{port}` URL the browser gets
opened to. There's no cross-process "announce the port back to the caller" step (as Hermes's
Electron main process waits on) — the same function that picks the port is the one that opens
the browser.

## 3. Serving the UI: static files, not a dev server

The React app (`apps/studio`) is a Vite-built SPA. In production it is **not** served by Vite —
Vite's dev server (`pnpm dev:studio`, port 5174) only exists for local frontend development.
What `main.py` actually does ([main.py:346-371](../packages/husk-studio-backend/src/husk_studio_backend/main.py:346)):

```python
@app.get("/", include_in_schema=False)
async def _spa_root() -> FileResponse:
    return FileResponse(studio_dist / "index.html")

@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_fallback(full_path: str) -> FileResponse:
    ...  # unmatched paths also fall back to index.html (client-side routing)

app.mount("/assets", StaticFiles(directory=studio_dist / "assets"), ...)
```

If `apps/studio/dist` doesn't exist yet (fresh clone), `_ensure_studio_built()` runs a one-shot
`pnpm --filter studio build` the first time, then never touches Node again. This is the
opposite of Hermes's Electron shell, which loads its renderer window against a **running**
dev server or bundled assets chosen at launch — here it's always prebuilt static output, with a
just-in-time build only as a first-run convenience.

## 4. Transport: relative-path REST + same-origin WebSocket

Because the SPA is served from the exact same origin as the API, the frontend code in
[api.ts](../apps/studio/client/src/lib/api.ts) never hardcodes a host:

```ts
const BASE = "/api/v1";
async function fetcher<T>(url: string) {
  const r = await fetch(url);          // relative — resolves against window.location
  ...
}
```

```ts
export function subscribeRun(id: string, onEvent: (e: RunEvent) => void): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/runs/${id}`);
  ws.onmessage = (msg) => onEvent(JSON.parse(msg.data) as RunEvent);
  return ws;
}
```

`window.location.host` is read live in the browser, so it self-corrects even when
`_resolve_port` bumped off 7654 onto a different port. There is no separate "get the gateway
URL" IPC call (Hermes's `getGatewayWsUrl()`) because there is nothing to resolve — the page's
own origin *is* the answer.

The only place a fixed backend URL is configured at all is Vite's **dev-only** proxy
([vite.config.ts:9-11](../apps/studio/vite.config.ts:9)), which forwards `/api` and `/ws` from
the Vite dev server (5174) to the backend (7654 by default, overridable via
`VITE_BACKEND_PORT`) so `pnpm dev:studio` can run frontend and backend as two processes during
development. That proxy config is not present in the production build; the built bundle
contains only the relative-path calls above.

## 5. Auth model: none — loopback binding is the boundary

There is no session token, no `?ticket=` handshake, no `window.hermesDesktop`-style bridge
gating access to the API. The trust boundary is **network-level, not application-level**:
`start_server()` binds `127.0.0.1` by default, and state-changing routes (ingest, replay,
debugger) additionally reject any peer that isn't loopback and reject cross-origin browser
requests — so a malicious web page you happen to have open in another tab can't drive them, but
any process on your own machine that can reach `127.0.0.1:{port}` can. This is deliberately
simpler than Hermes's per-request token auth because the deployment model is different: Husk is
single-user, one machine, one browser tab you opened yourself — there's no "renderer sandboxed
away from a trusted main process" boundary to enforce because there's no Electron main process
in the picture at all. The browser tab *is* the direct, first-class client of the API.

## Why this shape, compared to a desktop-wrapped design

The Hermes reference doc lists a boot handshake, port announcement, IPC proxying, and
token-gated local traffic as necessary complexity for its shape (Electron shell as a separate
process spawning and mediating access to a Python backend). None of that machinery exists here
because there is no separate shell process to broker access — `husk-studio start` *is* the backend,
serving its own UI directly, opened in whatever browser the OS already has. That's a deliberate
trade: less resilience machinery (no reconnect-on-wake-from-sleep, no OAuth refresh flow), in
exchange for a much smaller moving-parts count, consistent with Husk's local-first,
single-process design described in [architecture.md](architecture.md).
