# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the Husk Studio binary (CLI + backend + Studio UI).

Build with `uv run python packaging/build.py` (it builds the Studio bundle and a
clean environment first), or directly:

    corepack pnpm --filter studio build
    uv run --group build pyinstaller husk-studio.spec --noconfirm --clean

Output: dist/husk-studio/ (onedir, default). Set HUSK_STUDIO_ONEFILE=1 for a single
executable instead. PyInstaller cannot cross-compile; build on each target OS.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

ROOT = Path(SPECPATH)  # noqa: F821  (injected by PyInstaller)
STUDIO_DIST = ROOT / "apps" / "studio" / "dist"
ONEFILE = os.environ.get("HUSK_STUDIO_ONEFILE") == "1"

if not (STUDIO_DIST / "index.html").is_file():
    raise SystemExit(
        f"Studio bundle missing at {STUDIO_DIST}. Build it first:\n"
        "    corepack pnpm install --frozen-lockfile && corepack pnpm --filter studio build\n"
        "(or just run `uv run python packaging/build.py`)."
    )

# --- data files ------------------------------------------------------------
# The backend looks for the UI at husk_studio_backend/studio_static first
# (main.py:_find_studio_dist), so no path code is needed in the frozen app.
datas = [(str(STUDIO_DIST), "husk_studio_backend/studio_static")]
datas += collect_data_files("jsonschema_specifications")  # mcp -> jsonschema

# --- package metadata ------------------------------------------------------
# Read at runtime via importlib.metadata: our own version lookups, mcp's
# `version("mcp")`, and OpenTelemetry's entry-point plugin loading (it raises at
# import time without its dist-info).
for dist in (
    "husk-studio",
    "husk-studio-backend",
    "husk-studio-sandbox",
    "husk-studio-shared",
    "mcp",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-proto",
    "opentelemetry-semantic-conventions",
    "opentelemetry-exporter-otlp-proto-common",
    "opentelemetry-exporter-otlp-proto-http",
):
    datas += copy_metadata(dist)

# --- imports static analysis cannot see ------------------------------------
hiddenimports = []
for pkg in (
    "husk_studio",  # lazy imports inside click commands
    "husk_studio_backend",  # uvicorn gets "husk_studio_backend.main:app" as a string
    "husk_studio_shared",
    "husk_studio_sandbox",
    "uvicorn",  # loops / http / websockets / lifespan chosen by name at runtime
    "websockets",
    "sqlalchemy.dialects.sqlite",  # dialect picked from the URL scheme
    "opentelemetry.proto",  # generated *_pb2 modules
    "opentelemetry.exporter.otlp.proto.http",
    "mcp",
):
    # mcp.cli is the `mcp` dev CLI and needs the optional `typer` extra; nothing here uses it.
    hiddenimports += collect_submodules(
        pkg, filter=lambda name: not name.startswith("mcp.cli"), on_error="warn"
    )
hiddenimports += ["aiosqlite", "h11"]

# Heavy things that live in the `examples` group, never in the product.
excludes = [
    "tkinter",
    "pytest",
    "_pytest",
    "IPython",
    "matplotlib",
    "langchain",
    "langchain_core",
    "langchain_community",
    "openai",
    "groq",
    "cerebras",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(p) for p in sorted((ROOT / "packages").glob("*/src"))],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

if ONEFILE:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="husk-studio",
        console=True,
        upx=False,
    )
else:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="husk-studio",
        console=True,
        upx=False,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.datas,
        upx=False,
        name="husk-studio",
    )
