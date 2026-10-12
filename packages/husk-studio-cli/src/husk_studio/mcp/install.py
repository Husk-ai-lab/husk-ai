"""`husk-studio mcp install` — wire the Husk MCP server into a client's config.

Mirrors the IDE-hook installers (see packages-npm/husk-cursor-hook/src/install.ts):
resolve the client's config path, **merge** the `husk` entry into `mcpServers`
without clobbering other servers, refuse to overwrite an existing `husk` entry
unless `--force`, then print next steps.

Local clients (Claude Code, Cursor, Claude Desktop, Windsurf) launch the stdio
server via a command. Remote/cloud clients (Lovable) connect to a URL, so for
those we print tunnel instructions instead of writing a file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# Clients that connect over stdio with a local command + per-client config file.
STDIO_CLIENTS = ("claude-code", "cursor", "claude-desktop", "windsurf")
# Clients that connect to a URL (handled with printed instructions).
REMOTE_CLIENTS = ("lovable", "remote")
CLIENTS = STDIO_CLIENTS + REMOTE_CLIENTS


def _executable() -> str:
    """The command a client should launch. Inside a PyInstaller bundle that is the
    binary itself (it may not be on PATH); otherwise the `husk-studio` script."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("husk-studio") or "husk-studio"


def _stdio_block() -> dict[str, object]:
    """The MCP server entry. Prefer an absolute path so GUI clients (e.g. Claude
    Desktop) that don't inherit the shell PATH can still find `husk-studio`."""
    return {"command": _executable(), "args": ["mcp"]}


def _claude_desktop_config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "Claude"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "Claude"
    else:
        base = Path.home() / ".config" / "Claude"
    return base / "claude_desktop_config.json"


def _config_path(client: str, scope: str, target_dir: str | None) -> Path:
    root = Path(target_dir).expanduser() if target_dir else Path.cwd()
    home = Path.home()
    if client == "cursor":
        return (root / ".cursor" / "mcp.json") if scope == "project" else (home / ".cursor" / "mcp.json")
    if client == "claude-code":
        # Project scope writes the standard .mcp.json Claude Code reads.
        return root / ".mcp.json"
    if client == "claude-desktop":
        return _claude_desktop_config_path()
    if client == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    raise ValueError(f"unknown stdio client: {client}")


def _merge_into(path: Path, force: bool) -> str:
    block = _stdio_block()
    data: dict[str, object] = {}
    if path.exists():
        text = path.read_text("utf-8").strip()
        if text:
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError as e:
                return f"error: {path} is not valid JSON ({e}). Fix or remove it, then retry."
            if not isinstance(loaded, dict):
                return f"error: {path} does not contain a JSON object. Fix it by hand."
            data = loaded

    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        return f"error: {path} has a non-object 'mcpServers'. Fix it by hand."

    if "husk" in servers and not force:
        return (
            f"'husk' is already configured in {path}.\n"
            "Re-run with --force to overwrite it (other servers are preserved)."
        )

    servers["husk"] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    return f"Wrote husk MCP server to {path}\n\n{_next_steps_for(path)}"


def _next_steps_for(path: Path) -> str:
    return (
        "Next:\n"
        "  1. Ensure `husk-studio` is on PATH (or in the env your client launches).\n"
        f"  2. Restart your client so it reloads {path.name}.\n"
        "  3. Ask it to list tools — you should see `list_runs`, `get_trace`, `cost_breakdown`, …\n"
        "  4. (Optional) run `husk-studio start` for live data + to enable replay.\n"
        "\nReplay runs your agent code and is OFF by default. To turn it on for a client,\n"
        'set the server args to ["mcp", "--enable-replay"] (local use only).'
    )


def _remote_instructions() -> str:
    return (
        "Lovable and other remote/cloud clients connect to a URL, not a local command.\n\n"
        "  1. Start the HTTP transport:\n"
        "       husk-studio mcp --transport http        # binds 127.0.0.1:7655, endpoint /mcp\n"
        "  2. Expose it with a tunnel:\n"
        "       cloudflared tunnel --url http://127.0.0.1:7655\n"
        "       # or: ngrok http 7655\n"
        "  3. In the client (e.g. Lovable), add an MCP server pointing at:\n"
        "       https://<your-tunnel-host>/mcp\n\n"
        "Security: only read tools are exposed by default. Replay stays OFF over HTTP\n"
        "unless you add --enable-replay, which executes your agent code — keep that local."
    )


def install(
    *, client: str, scope: str = "user", target_dir: str | None = None, force: bool = False
) -> str:
    """Install/print the Husk MCP config for `client`. Returns a message to print."""
    client = client.lower()
    if client in REMOTE_CLIENTS:
        return _remote_instructions()
    if client not in STDIO_CLIENTS:
        raise ValueError(f"unknown client {client!r}; choose from {', '.join(CLIENTS)}")

    # Claude Code's user scope is best managed by its own CLI; project scope
    # writes a portable .mcp.json.
    if client == "claude-code" and scope == "user":
        exe = _executable()
        quoted = f'"{exe}"' if " " in exe else exe
        return (
            "Claude Code (user scope): run\n"
            f"    claude mcp add husk -- {quoted} mcp\n\n"
            "Or use `--scope project` to write a .mcp.json into the current project."
        )

    path = _config_path(client, scope, target_dir)
    return _merge_into(path, force=force)
