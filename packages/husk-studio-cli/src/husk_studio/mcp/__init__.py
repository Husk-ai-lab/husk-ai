"""Husk MCP server — exposes runs, traces, and replay over the Model Context
Protocol so coding agents (Claude Code, Cursor, Claude Desktop, Windsurf,
Lovable, …) can introspect and replay agent runs.

The server is launched via `husk-studio mcp`. Everything here is lazy-imported from
the CLI so the heavier MCP/SDK dependencies only load when actually serving.
"""

from __future__ import annotations

from husk_studio.mcp.server import build_server, serve

__all__ = ["build_server", "serve"]
