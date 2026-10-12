from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from husk_studio_backend import __version__
from husk_studio_backend.api import (
    branches,
    cursor,
    dashboard,
    debugger,
    diff,
    graph,
    integrations,
    otel,
    replay,
    runs,
    spans,
)
from husk_studio_backend.api._guard import local_only
from husk_studio_backend.db.engine import init_db

log = logging.getLogger(__name__)


def _find_studio_dist() -> Path | None:
    """Locate the bundled studio (apps/studio/dist) for production serving.

    Two search paths, in order:
      1. Inside the installed wheel: husk_studio_backend/studio_static/
         (populated at wheel-build time by `scripts/bundle_studio.py`).
      2. Relative to the workspace checkout: ../../../apps/studio/dist
         (used when running from a `uv sync` clone without a wheel).
    Returns the first path that has index.html, or None.

    In a PyInstaller bundle only the first path applies: `__file__` is virtual
    there, so the workspace path would resolve to wherever the binary happens to
    sit and could serve a stray checkout's bundle instead of the baked-in one.
    """
    here = Path(__file__).resolve().parent
    candidates = [here / "studio_static"]
    if not getattr(sys, "frozen", False):
        candidates.append(here.parents[3] / "apps" / "studio" / "dist")
    for cand in candidates:
        if (cand / "index.html").is_file():
            return cand
    return None


def _ensure_studio_built() -> None:
    """Build the studio bundle if running from a workspace checkout without one.

    Idempotent. Skipped when:
      * Bundle already exists (wheel install OR previous build).
      * HUSK_NO_AUTO_BUILD=1 (tests, CI, devs who want to debug the fallback).
      * Not a workspace checkout (no apps/studio/package.json).
      * `corepack` not on PATH (no Node — user sees the landing page with
        instructions instead).
    Falls back to the landing HTML on subprocess failure (e.g. missing
    node_modules, vite crash, timeout).
    """
    if os.environ.get("HUSK_NO_AUTO_BUILD") == "1":
        return
    if getattr(sys, "frozen", False):
        return  # a frozen binary ships its bundle; it is never a workspace checkout
    if _find_studio_dist() is not None:
        return

    here = Path(__file__).resolve().parent
    workspace_root = here.parents[3]
    studio_pkg = workspace_root / "apps" / "studio" / "package.json"
    if not studio_pkg.is_file():
        return  # wheel install or otherwise outside the workspace

    # shutil.which handles Windows PATHEXT (resolves to corepack.cmd) so we
    # don't need shell=True and the brittle quoting that comes with it.
    corepack = shutil.which("corepack")
    if corepack is None:
        log.warning(
            "Studio bundle not found and `corepack` is not on PATH. "
            "Serving the landing page. Install Node 20+ then run "
            "`corepack pnpm --filter studio build` to bake the bundle."
        )
        return

    log.info(
        "Studio bundle not found; building via `corepack pnpm --filter "
        "studio build` (~10-30s on first run; subsequent boots are instant)..."
    )
    try:
        subprocess.run(
            [corepack, "pnpm", "--filter", "studio", "build"],
            cwd=workspace_root,
            check=True,
            timeout=180,
        )
        log.info("Studio bundle built.")
    except subprocess.CalledProcessError as exc:
        log.warning(
            "Studio auto-build failed (exit %d). Serving landing page. "
            "Likely cause: missing node_modules. From the repo root run "
            "`corepack pnpm install && corepack pnpm --filter studio build` "
            "then restart `husk-studio start`.",
            exc.returncode,
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "Studio auto-build timed out after 180s. Serving landing page. "
            "Run `corepack pnpm --filter studio build` manually."
        )
    except OSError as exc:
        log.warning("Studio auto-build OS error: %s. Serving landing page.", exc)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await init_db()
    log.info(
        "husk-studio is a local development debugger. It observes only the agent you run on this "
        "machine (ingest is loopback-only) — never a production deployment elsewhere."
    )
    yield


app = FastAPI(
    title="Husk Studio Backend",
    version=__version__,
    description="Local API + WebSocket for the Husk visual debugger.",
    lifespan=lifespan,
)

# The Studio (Vite) runs at :5174 in dev. In a packaged build the backend itself
# serves the Studio bundle from `/`.
app.add_middleware(
    CORSMiddleware,
    # Loopback dev origin only (the Studio's Vite dev server). State-changing
    # routes are further gated to loopback by the local_only guard.
    allow_origins=[
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Local single-user surfaces. Husk is a local-first development debugger: the only
# client is the Studio running on this machine. Read views are open on loopback;
# anything that ingests, runs code, or writes to disk is gated to loopback by the
# local_only guard below.
app.include_router(runs.router)
app.include_router(spans.router)
app.include_router(branches.router)
app.include_router(diff.router)
# Ingest accepts traces only from the agent you're debugging on this machine. This
# loopback-only gate is what keeps Husk a DEVELOPMENT tool, not production monitoring:
# a production agent on another host is refused, so Husk can't be pointed at prod.
app.include_router(otel.router, dependencies=[Depends(local_only)])
app.include_router(cursor.router)
app.include_router(replay.router, dependencies=[Depends(local_only)])
app.include_router(integrations.router)
app.include_router(dashboard.router)
app.include_router(graph.router)
# The debugger resolves the local BYOK key and can write a fix to disk: loopback only.
app.include_router(debugger.router, dependencies=[Depends(local_only)])


_LANDING_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Husk - Studio not built yet</title>
    <style>
      /* "Ink on paper" — same OKLCH tokens as the Studio (neutrals at hue 292, chroma
         <= 0.02). Hierarchy is tone; the one chromatic note is the brand violet. */
      :root { color-scheme: light; }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", sans-serif;
        background: oklch(0.985 0.006 292);
        color: oklch(0.22 0.006 292);
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 2rem;
        line-height: 1.55;
      }
      .wrap { max-width: 720px; width: 100%; }
      h1 {
        margin: 0 0 0.5rem;
        font-size: 1.875rem;
        letter-spacing: -0.02em;
        font-weight: 700;
      }
      .accent { color: oklch(0.575 0.225 286); }
      .lead { color: oklch(0.50 0.008 292); margin: 0 0 2rem; font-size: 1rem; }
      .grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 1rem;
      }
      @media (min-width: 720px) {
        .grid { grid-template-columns: 1fr 1fr; }
      }
      .card {
        border: 1px solid oklch(0.885 0.006 292);
        background: oklch(0.998 0.006 292);
        padding: 1.5rem;
        border-radius: 12px;
        transition: border-color .2s;
      }
      .card:hover { border-color: oklch(0.575 0.225 286 / 0.4); }
      .card h2 {
        margin: 0 0 0.25rem;
        font-size: 1.125rem;
        font-weight: 600;
      }
      .card .sub {
        margin: 0 0 1rem;
        font-size: 0.8125rem;
        color: oklch(0.50 0.008 292);
      }
      .eyebrow {
        font-size: 0.6875rem;
        text-transform: uppercase;
        letter-spacing: 0.18em;
        color: oklch(0.22 0.006 292);
        font-weight: 600;
        margin: 0 0 0.5rem;
      }
      code, pre {
        font-family: "Geist Mono", "JetBrains Mono", "Courier New", monospace;
        font-size: 0.8125rem;
      }
      pre {
        background: oklch(0.955 0.006 292);
        border: 1px solid oklch(0.885 0.006 292);
        border-radius: 6px;
        padding: 0.625rem 0.75rem;
        margin: 0.375rem 0;
        overflow-x: auto;
        white-space: pre-wrap;
        word-break: break-word;
      }
      pre .prompt { color: oklch(0.50 0.008 292); user-select: none; }
      .footer {
        margin-top: 2rem;
        padding-top: 1.25rem;
        border-top: 1px solid oklch(0.885 0.006 292);
        font-size: 0.8125rem;
        color: oklch(0.50 0.008 292);
      }
      a { color: oklch(0.575 0.225 286); text-decoration: none; }
      a:hover { text-decoration: underline; }
      .why {
        margin-top: 1.25rem;
        padding: 0.875rem 1rem;
        border-radius: 8px;
        background: oklch(0.955 0.006 292);
        border: 1px solid oklch(0.885 0.006 292);
        font-size: 0.8125rem;
        color: oklch(0.50 0.008 292);
      }
    </style>
  </head>
  <body>
    <div class="wrap">
      <p class="eyebrow">Backend is running on port __PORT__</p>
      <h1>Husk <span class="accent">Studio</span> isn't built yet.</h1>
      <p class="lead">
        The API is live but the UI bundle is missing. Pick one of the two options below.
      </p>

      <div class="grid">
        <div class="card">
          <p class="eyebrow">Option 1</p>
          <h2>Build the bundle once</h2>
          <p class="sub">Permanent. Studio loads from <code>/</code> after this.</p>
          <pre><span class="prompt">$ </span>corepack pnpm install</pre>
          <pre><span class="prompt">$ </span>corepack pnpm --filter studio build</pre>
          <p class="sub" style="margin-top:0.75rem">
            Then <a href="/">reload this page</a>.
          </p>
        </div>

        <div class="card">
          <p class="eyebrow">Option 2</p>
          <h2>Dev server (hot reload)</h2>
          <p class="sub">For editing Studio source. Vite at <code>:5174</code>, proxies <code>/api</code> back here.</p>
          <pre><span class="prompt">$ </span>corepack pnpm dev:studio</pre>
          <p class="sub" style="margin-top:0.75rem">
            Then open <a href="http://localhost:5174">http://localhost:5174</a>.
          </p>
        </div>
      </div>

      <div class="why">
        <strong style="color:oklch(0.22 0.006 292)">Why am I seeing this page?</strong>
        Either (a) you cloned the repo and haven't built the Studio yet, OR
        (b) auto-build tried and failed (check the terminal where you ran
        <code>husk-studio start</code> for the warning). Both options above need
        Node.js 20+ and <code>corepack</code> enabled. See the
        <a href="https://github.com/husk-ai-lab/husk-ai#build-the-studio--run-a-dev-server">full guide</a>.
      </div>

      <p class="footer">
        API health: <a href="/api/health">/api/health</a>
        &nbsp;&middot;&nbsp; Backend version: __VERSION__
        &nbsp;&middot;&nbsp; Need Node?
        <a href="https://nodejs.org">install guide</a>
      </p>
    </div>
  </body>
</html>
"""


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "husk-studio-backend", "version": __version__}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    # Back-compat alias for older clients.
    return {"ok": True}


# Mount the bundled studio at /. If we're in a workspace checkout without a
# dist/, try to build it once before falling back to the landing page (no-op
# in wheel installs and when HUSK_NO_AUTO_BUILD=1).
_ensure_studio_built()
_studio_dist = _find_studio_dist()
if _studio_dist is not None:
    studio_dist = _studio_dist  # narrowed to Path for the closures below
    log.info("Serving studio bundle from %s", studio_dist)

    # The SPA shell must always be revalidated. Without this the browser
    # caches it heuristically and, after a rebuild, boots the previous
    # bundle — which then calls endpoints this backend no longer serves.
    # The ETag keeps the revalidation a cheap 304; hashed assets under
    # /assets are immutable by name, so they don't need it.
    def _shell() -> FileResponse:
        return FileResponse(
            studio_dist / "index.html",
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        return _shell()

    # SPA fallback: any unknown route under / that isn't an API/WS/asset
    # request returns index.html so wouter / react-router can handle it.
    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str) -> FileResponse:
        # Static asset hit (assets/* served by the mount below) — let the
        # mount handle it. We only fall through here on misses.
        if full_path.startswith(("api/", "ws/", "v1/")):
            # Should be matched by routers; if we get here it's a real 404.
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        asset = studio_dist / full_path
        if asset.is_file():
            return FileResponse(asset)
        return _shell()

    # The Vite build emits hashed assets under /assets/*.
    app.mount(
        "/assets",
        StaticFiles(directory=studio_dist / "assets"),
        name="studio-assets",
    )
else:
    log.info("No studio bundle found; serving dev landing page at /")

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request) -> HTMLResponse:
        port = str(request.url.port or 7654)
        return HTMLResponse(
            _LANDING_HTML
            .replace("__VERSION__", __version__)
            .replace("__PORT__", port)
        )


