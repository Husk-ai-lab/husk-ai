# Reference: Hermes Agent (NousResearch) — Electron ↔ Python Backend Connection

> **What this is:** notes from public documentation/source on how
> [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)'s desktop app wires
> its Electron shell to its Python backend. This is an **external reference**, kept for
> comparison against Husk's own architecture (see
> [frontend-backend-connection-architecture.md](frontend-backend-connection-architecture.md)) —
> none of this describes code in this repo, and none of it is a recommendation to adopt Electron.
>
> Sources: [hermes-agent apps/desktop](https://github.com/NousResearch/hermes-agent/tree/main/apps/desktop),
> [DeepWiki: Desktop App](https://deepwiki.com/NousResearch/hermes-agent/3.5-desktop-app),
> [DeepWiki: Electron Shell and Gateway Connection](https://deepwiki.com/NousResearch/hermes-agent/12.1-electron-shell-and-gateway-connection),
> [Issue #52662 — backend timeout](https://github.com/NousResearch/hermes-agent/issues/52662),
> [Issue #49982 — Windows connection timeout](https://github.com/NousResearch/hermes-agent/issues/49982).

## Overall shape

Hermes Agent's desktop app is an Electron shell wrapped around the same CLI/gateway backend
used headlessly and by the browser dashboard. Unlike a typical Electron app that bundles its
own Node backend, Hermes's actual application logic lives in a **Python process** that the
Electron main process locates or spawns — Electron here is a native shell + IPC broker, not
where the business logic runs.

```
Electron main process              Python backend ("hermes serve")
┌─────────────────────┐            ┌─────────────────────────────┐
│ runBootstrap()       │  spawns   │ FastAPI gateway              │
│  → resolves runtime   │──────────▶│  tui_gateway JSON-RPC/WS API │
│  → child_process      │           │  dynamic dashboard port      │
├─────────────────────┤            │  (announced back to Electron)│
│ waitForHermesReady()  │◀──health──┤                              │
│ waitForDashboardPort  │  checks   └─────────────────────────────┘
│  Announcement()       │                    ▲
├─────────────────────┤                    │ HTTP + WebSocket (JSON-RPC)
│ preload.ts            │                    │
│  window.hermesDesktop  │────────────────────┘
├─────────────────────┤
│ React renderer         │  (via apps/shared, same transport as
│  (contextIsolated)     │   the browser dashboard uses)
└─────────────────────┘
```

## Runtime resolution (before anything is spawned)

`electron/main.ts` resolves a runnable Hermes runtime through an ordered fallback ladder,
probing each candidate rather than trusting its presence:

1. `HERMES_DESKTOP_HERMES_ROOT` env var
2. Current source checkout (dev mode)
3. A managed local install
4. `HERMES_DESKTOP_HERMES` env var, or `hermes` on `PATH`
5. System Python with the Hermes runtime available
6. First-launch bootstrap installer (last resort — installs one)

## Process spawning + readiness

- `runBootstrap()` spawns the resolved Hermes agent as a **child process** from the Electron
  main process (not the renderer).
- Before any UI depends on the connection, the main process runs sequential health checks:
  `waitForHermesReady()` and `waitForDashboardPortAnnouncement()` — the backend announces its
  bound dashboard port dynamically (it isn't hardcoded), and Electron waits for that
  announcement rather than guessing.
- `execProbeSync` and `probeGatewayWebSocket()` additionally test the WebSocket endpoint
  (including auth) for reachability before any renderer window is allowed to depend on the URL.
- This matters in practice: both linked GitHub issues (#52662, #49982) are reports of the
  desktop app timing out waiting for this handshake to complete (15s timeout observed on
  Windows), i.e. the boot-and-probe sequence is a real failure surface in this design.

## Protocol stack

Three layers, not one:

- **HTTP(S)** — OAuth token requests, made via Electron's own `net` module (not the renderer's
  `fetch`), so the main process controls outbound auth traffic.
- **WebSocket** — the primary bidirectional channel to the gateway, used for streaming
  responses and live tool activity.
- **JSON-RPC** — the message *format* carried over that WebSocket (procedural calls, not a REST
  shape).

## Auth: session tokens, two modes

- **Legacy token mode** — a token passed directly as a `?token=` query param on the WebSocket
  URL.
- **OAuth mode** — a `?ticket=` single-use WebSocket ticket, obtained via `POST
  /api/auth/ws-ticket`, handled by an `oauth-net-request.ts` wrapper that manages token
  persistence, refresh, and expiry via a `NativeTokenSet`.
- An ephemeral `HERMES_DASHBOARD_SESSION_TOKEN` is minted at startup specifically to secure the
  *internal* API surface — i.e. even loopback-only traffic between Electron and its own spawned
  backend is authenticated, not trusted by virtue of being localhost.

## IPC: how the renderer actually talks to Python

This is the part most different from a plain web app: the **renderer never calls the Python
backend directly**. Electron's context-isolated `preload.ts` injects a narrow
`window.hermesDesktop` namespace — `getConnection()`, `getGatewayWsUrl()`, OAuth flows,
connection-config CRUD — and an **IPC handler in the main process proxies the actual HTTP/WS
requests** via `http.request()`. The renderer asks the main process "get me a connection /
give me the WS URL"; the main process is the one holding the socket to Python. This keeps raw
network access out of the (sandboxed, context-isolated) renderer entirely — a deliberate
Electron security boundary, not an accident of how it was built.

## Multi-topology connection config

`connection-config.ts` normalizes several deployment shapes behind one interface:

- **Local** — `HERMES_HOME` forces spawning a local backend (the default desktop case above).
- **Remote gateway** — `normalizeRemoteBaseUrl()` for a user-supplied HTTPS gateway URL, using
  the same auth path as the "Hermes Cloud" hosted offering.
- **SSH tunneling** — per-profile SSH config for proxied remote access.

`applyConnectionChange()` is the single function that switches between spawning a local backend
and pointing at a remote one, updating shared state across every open renderer window.

## Resilience

`revalidateConnection()` re-checks backend liveness on wake-from-sleep and recovers from
transient network failures; OAuth expiry surfaces as a login dialog over IPC rather than a
silent failure or crash.

## Takeaways relevant to Husk (if a desktop shell is ever considered)

1. **The backend port is discovered, not assumed** — Electron waits for the Python process to
   announce its bound port, the same problem Husk's CLI already solves for the browser case via
   `_resolve_port` (see [server.py](../packages/husk-cli/src/husk/server.py)).
2. **A boot handshake (spawn → health-check → probe) is load-bearing** and is exactly where
   Hermes's own bug reports cluster — worth designing explicit timeouts/error UI around, not an
   afterthought.
3. **Even fully local, same-machine traffic is token-authenticated** in Hermes — loopback-only
   binding (Husk's current model) is a different, simpler trust boundary; a desktop-wrapped
   future version would need to decide which model it wants.
4. **The renderer is deliberately not given direct network access** — all backend calls are
   proxied through the main process. This is an Electron-specific security posture that has no
   equivalent in Husk's current pure-browser model (the browser tab *is* the direct client of
   the API today).
