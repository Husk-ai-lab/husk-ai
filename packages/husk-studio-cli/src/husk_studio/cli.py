from __future__ import annotations

import logging
import os
import sys
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from husk_studio import __version__
from husk_studio.config import db_path, husk_home


def _force_utf8_output() -> None:
    """Make our output survive a non-UTF-8 stdout.

    On Windows, a redirected or piped stdout falls back to the legacy ANSI code
    page (cp1252), which cannot encode the arrows and middots this CLI prints.
    `husk-studio export --out FILE | tail` then died with UnicodeEncodeError *after*
    writing the file — data intact, but a non-zero exit that breaks any script or
    CI step wrapping it. Degrade unencodable characters instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # not a TextIOWrapper (e.g. captured in tests)
            pass


_force_utf8_output()
console = Console()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )


def _default_url() -> str:
    """Backend URL for client commands: the port the last `husk-studio start` bound
    (written to ~/.husk/port), falling back to 7654."""
    try:
        port = int((husk_home() / "port").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        port = 7654
    return f"http://127.0.0.1:{port}"


def _child_env() -> dict[str, str]:
    """Environment for the user's agent process.

    A PyInstaller bundle prepends its own directory to the dynamic-library path so
    its bundled libs load; the child (the user's own Python, node, ...) must not
    inherit that, or it can pick up our libssl/libz instead of its own. The
    bootloader keeps the original value in `<VAR>_ORIG`.
    """
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            orig = env.pop(f"{var}_ORIG", None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
    return env


@click.group()
@click.version_option(__version__, prog_name="husk-studio")
@click.option(
    "--log",
    "log_level",
    default="info",
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
    envvar="HUSK_LOG",
    help="Log verbosity (env HUSK_LOG).",
)
def main(log_level: str) -> None:
    """Husk — the visual debugger for AI agents."""
    _setup_logging(log_level)


@main.command()
@click.option("--port", default=7654, type=int, help="Backend port (default 7654).")
@click.option("--host", default="127.0.0.1", help="Bind host.")
@click.option("--no-open-browser", is_flag=True, help="Do not auto-open the browser.")
def start(port: int, host: str, no_open_browser: bool) -> None:
    """Boot the Husk backend and open the Studio in your browser."""
    from husk_studio.server import start_server

    try:
        start_server(host=host, port=port, open_browser=not no_open_browser)
    except KeyboardInterrupt:
        console.print("\n[dim]Husk Studio stopped.[/dim]")
        sys.exit(0)


@main.command(name="list")
@click.option("--limit", default=20, type=int)
def list_runs(limit: int) -> None:
    """List recent runs."""
    from husk_studio_backend.db.engine import sync_engine, sync_session
    from husk_studio_backend.db.models import Base, RunRow

    Base.metadata.create_all(sync_engine())  # same guard as export/delete: a fresh ~/.husk has no tables
    with sync_session() as s:
        rows = s.query(RunRow).order_by(RunRow.started_at.desc()).limit(limit).all()

    table = Table(title=f"Husk Studio runs ({len(rows)})")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("framework")
    table.add_column("status")
    table.add_column("tokens")
    table.add_column("cost (USD)")
    for r in rows:
        table.add_row(
            r.id,
            r.framework or "-",
            r.status,
            f"{(r.total_tokens_in or 0) + (r.total_tokens_out or 0)}",
            f"${r.total_cost_usd or 0:.4f}",
        )
    console.print(table)


@main.command()
def doctor() -> None:
    """Diagnostics: versions, paths, integration health."""
    import importlib.util

    home = husk_home()
    db = db_path()
    console.print(f"husk-studio: [cyan]{__version__}[/cyan]")
    console.print(f"home: {home}")
    if db.exists():
        console.print(f"db:   {db}  [green]ok[/green]")
    else:
        console.print(f"db:   {db}  [yellow]missing (created on first `husk-studio start`)[/yellow]")

    # MCP server status — is the SDK present, and is replay gated?
    if importlib.util.find_spec("mcp") is not None:
        console.print("mcp:  [green]ready[/green]  connect with: [cyan]husk-studio mcp[/cyan]")
    else:
        console.print("mcp:  [red]'mcp' package missing[/red] — reinstall husk-studio")
    replay_on = os.environ.get("HUSK_MCP_ENABLE_REPLAY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    state = "[yellow]enabled[/yellow]" if replay_on else "disabled (read-only)"
    console.print(
        f"      replay: {state} "
        "[dim](--enable-replay or HUSK_MCP_ENABLE_REPLAY=1; local-only)[/dim]"
    )

    console.print("\n[dim]Next steps:[/dim]")
    console.print("  [cyan]husk-studio start[/cyan]        backend + Studio")
    console.print("  [cyan]husk-studio demo[/cyan]         seed a sample run")
    console.print("  [cyan]husk-studio run <cmd>[/cyan]    capture your own agent")


@main.command()
@click.option("--url", default=None, help="Husk Studio backend URL (default: the running backend's port).")
def demo(url: str | None) -> None:
    """Seed demo fixtures so the Studio has a fresh narrative to show.

    Emits a 3-span OTel trace and a sample IDE observability event so the
    Dashboard, /runs, and timeline have realistic data to render. Requires
    `husk-studio start` to be running.
    """
    import httpx

    base = (url or _default_url()).rstrip("/")

    try:
        h = httpx.get(f"{base}/api/health", timeout=2.0)
        h.raise_for_status()
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[red]Husk Studio backend not reachable at {base}[/red]\n"
            f"Start it first: [cyan]husk-studio start[/cyan]\n({e})"
        )
        sys.exit(1)

    # 1. Sample IDE observability event so the Cursor integration tile shows
    #    "live" on the Dashboard.
    cursor_payload = {
        "hook": "afterFileEdit",
        "project": "husk-demo",
        "payload": {
            "file_path": "src/agent/planner.py",
            "conversation_id": "demo-conv-1",
        },
    }
    try:
        r = httpx.post(f"{base}/api/cursor/events", json=cursor_payload, timeout=5.0)
        r.raise_for_status()
        event_id = r.json().get("event_id")
        console.print(f"  · seeded IDE observability event [dim](id={event_id})[/dim]")
    except Exception as e:  # noqa: BLE001
        console.print(f"    [yellow]IDE event skipped: {e}[/yellow]")

    # 2. Self-contained OTel trace emission — no extra deps beyond what
    #    husk-studio already installs (opentelemetry-sdk + OTLP/HTTP exporter).
    #    Emits a 3-span agent.run with GenAI v1.36 attributes so the run
    #    appears in /runs with realistic prompts, completions, and costs.
    console.print("  · emitting demo trace (3 spans, GenAI v1.36 attrs)…")
    try:
        _emit_demo_trace(base)
    except Exception as e:  # noqa: BLE001
        console.print(f"    [yellow]demo trace skipped: {e}[/yellow]")

    console.print("\n[green]Demo data ready.[/green]")
    console.print(f"Open the Studio: [cyan]{base}[/cyan]")
    console.print(
        "[dim]The Dashboard shows the run under /runs and the IDE event on the Cursor tile.[/dim]"
    )
    console.print(
        "\n[dim]Next — let your coding agent debug runs for you:[/dim] "
        "[cyan]husk-studio mcp install --client claude-code[/cyan] "
        "[dim](or cursor · windsurf); paste-in prompt in[/dim] [cyan]docs/mcp.md[/cyan]"
    )


def _emit_demo_trace(base: str) -> None:
    """Send a fully-formed OTel trace to the local Husk backend.

    No reliance on the examples/ directory — works out of the box for users
    who installed via `pip install husk-studio`.
    """
    import random as _random
    import time as _time

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    endpoint = f"{base.rstrip('/')}/v1/traces"
    resource = Resource.create({"service.name": "husk-ai-demo"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    # Use a fresh tracer namespace to avoid clashing with any global setup.
    tracer = provider.get_tracer("husk.cli.demo")

    with tracer.start_as_current_span("agent.run") as root:
        root.set_attribute("service.name", "husk-ai-demo")
        root.set_attribute("gen_ai.system", "openai")

        with tracer.start_as_current_span("chat gpt-4o-mini (plan)") as s:
            s.set_attribute("gen_ai.system", "openai")
            s.set_attribute("gen_ai.operation.name", "chat")
            s.set_attribute("gen_ai.request.model", "gpt-4o-mini")
            s.set_attribute("gen_ai.response.model", "gpt-4o-mini")
            s.set_attribute("gen_ai.usage.input_tokens", 42)
            s.set_attribute("gen_ai.usage.output_tokens", 18)
            s.add_event("gen_ai.user.message", {
                "content": "What's the capital of Italy and how many people live there?",
            })
            s.add_event("gen_ai.choice", {
                "finish_reason": "stop",
                "message.content": "I'll look up the population, then answer.",
            })
            _time.sleep(_random.uniform(0.05, 0.12))

        with tracer.start_as_current_span("tool: web_search") as s:
            s.set_attribute("gen_ai.tool.name", "web_search")
            s.set_attribute("gen_ai.tool.type", "function")
            s.add_event("gen_ai.tool.message", {
                "name": "web_search",
                "arguments": '{"q": "Rome population"}',
                "content": "Rome (Roma), Italy. Population: ~2.87M (2024).",
            })
            _time.sleep(_random.uniform(0.02, 0.05))

        with tracer.start_as_current_span("chat gpt-4o (answer)") as s:
            s.set_attribute("gen_ai.system", "openai")
            s.set_attribute("gen_ai.operation.name", "chat")
            s.set_attribute("gen_ai.request.model", "gpt-4o")
            s.set_attribute("gen_ai.response.model", "gpt-4o")
            s.set_attribute("gen_ai.usage.input_tokens", 88)
            s.set_attribute("gen_ai.usage.output_tokens", 64)
            s.add_event("gen_ai.user.message", {
                "content": "Compose the final answer using the search result.",
            })
            s.add_event("gen_ai.choice", {
                "finish_reason": "stop",
                "message.content": "Rome is the capital of Italy; ~2.87 million people live there.",
            })
            _time.sleep(_random.uniform(0.05, 0.12))

    provider.shutdown()


def _backend_healthy(base: str) -> bool:
    import httpx

    try:
        return httpx.get(f"{base}/api/health", timeout=1.0).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@main.command(context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option("--port", default=7654, type=int, help="Backend port (default 7654).")
@click.option("--service-name", default=None, help="OTel service.name (default: the command's name).")
@click.option(
    "--no-serve",
    is_flag=True,
    help="If Husk auto-started the backend, exit when the command finishes (for CI).",
)
def run(command: tuple[str, ...], port: int, service_name: str | None, no_serve: bool) -> None:
    """Run your agent and capture it: `husk-studio run python my_agent.py`.

    Ensures the Husk backend is up (auto-starts it if needed), points your agent's
    OpenTelemetry exporter at Husk via $OTEL_EXPORTER_OTLP_ENDPOINT, runs the
    command, and prints the Studio URL.
    """
    import subprocess
    import threading
    import time
    from pathlib import Path

    base = f"http://127.0.0.1:{port}"
    started_here = False

    if not _backend_healthy(base):
        import uvicorn

        from husk_studio.server import _resolve_port, _write_port_file
        from husk_studio_backend.main import app

        actual_port = _resolve_port("127.0.0.1", port)
        _write_port_file(actual_port)
        base = f"http://127.0.0.1:{actual_port}"
        cfg = uvicorn.Config(app, host="127.0.0.1", port=actual_port, log_level="warning")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True, name="husk-backend").start()
        for _ in range(100):  # up to ~5s for the socket to come up
            if _backend_healthy(base):
                break
            time.sleep(0.05)
        started_here = True
        console.print(f"[dim]Started Husk Studio backend on {base}[/dim]")

    env = _child_env()
    env["OTEL_EXPORTER_OTLP_ENDPOINT"] = base
    env.setdefault("OTEL_SERVICE_NAME", service_name or Path(command[0]).stem or "husk-agent")

    console.print(f"[dim]Running:[/dim] {' '.join(command)}")
    try:
        code = subprocess.run(list(command), env=env).returncode
    except FileNotFoundError:
        console.print(f"[red]Command not found:[/red] {command[0]}")
        sys.exit(127)

    console.print(f"\n[green]Done.[/green] View runs: [cyan]{base}/runs[/cyan]")

    if started_here and not no_serve:
        console.print("[dim]Backend still serving so you can view the run. Ctrl+C to stop.[/dim]")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    sys.exit(code)


@main.command()
@click.argument("run_id")
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="State override (repeatable). VALUE is parsed as JSON, else kept as a string.",
)
@click.option("--span", "span_id", default=None, help="Span id to fork from (re-run that node onward).")
@click.option("--cassette", is_flag=True, help="Serve LLM calls from the recorded HTTP cassette (model-free).")
@click.option("--url", default=None, help="Husk Studio backend URL (default: the running backend's port).")
def replay(
    run_id: str, overrides: tuple[str, ...], span_id: str | None, cassette: bool, url: str | None
) -> None:
    """Replay a recorded run with a modified state, from the terminal/CI.

    Example: husk-studio replay <run_id> --set topic=Tokyo --cassette
    """
    import json as _json

    import httpx

    state_override: dict[str, Any] = {}
    for item in overrides:
        if "=" not in item:
            console.print(f"[red]--set expects KEY=VALUE, got:[/red] {item}")
            sys.exit(2)
        k, _, v = item.partition("=")
        try:
            state_override[k] = _json.loads(v)
        except ValueError:
            state_override[k] = v

    base = (url or _default_url()).rstrip("/")
    body = {
        "run_id": run_id,
        "span_id": span_id,
        "state_override": state_override,
        "use_cassette": cassette,
    }
    try:
        r = httpx.post(f"{base}/api/replay", json=body, timeout=120.0)
    except httpx.ConnectError:
        console.print(
            f"[red]Husk Studio backend not reachable at {base}[/red]\n"
            f"Start it first: [cyan]husk-studio start[/cyan]"
        )
        sys.exit(1)
    if r.status_code >= 400:
        console.print(f"[red]Replay failed ({r.status_code}):[/red] {r.text}")
        sys.exit(1)
    data = r.json()
    console.print("[green]Replay started.[/green]")
    console.print(f"  thread: {data.get('thread_id')}")
    console.print(f"  child:  {data.get('child_id')}")
    console.print(f"  View:   [cyan]{base}/runs[/cyan] (the new run appears once OTel flushes)")


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Dump every mapped column of a SQLAlchemy row by its attribute name."""
    from sqlalchemy import inspect as _sa_inspect

    return {a.key: getattr(row, a.key) for a in _sa_inspect(row).mapper.column_attrs}


@main.command()
@click.argument("run_id")
@click.option("--out", "out_path", default=None, type=click.Path(), help="Write to FILE (default: stdout).")
def export(run_id: str, out_path: str | None) -> None:
    """Export a run (run + spans + branches) to a portable JSON bundle.

    The recorded text is already secret-redacted at ingest. Useful for bug reports
    and sharing a trajectory without exposing the whole database.
    """
    import json as _json
    from pathlib import Path

    from husk_studio_backend.db.engine import sync_engine, sync_session
    from husk_studio_backend.db.models import Base, BranchRow, RunRow, SpanRow

    Base.metadata.create_all(sync_engine())  # no-op if present; avoids a raw error on a fresh ~/.husk
    with sync_session() as s:
        run = s.get(RunRow, run_id)
        if run is None:
            console.print(f"[red]Run not found:[/red] {run_id}")
            sys.exit(1)
        spans = (
            s.query(SpanRow)
            .filter(SpanRow.run_id == run_id)
            .order_by(SpanRow.started_at.asc())
            .all()
        )
        branches = (
            s.query(BranchRow)
            .filter(
                (BranchRow.parent_run_id == run_id) | (BranchRow.child_run_id == run_id)
            )
            .all()
        )
        bundle = {
            "husk_export_version": 1,
            "husk_version": __version__,
            "run": _row_to_dict(run),
            "spans": [_row_to_dict(sp) for sp in spans],
            "branches": [_row_to_dict(b) for b in branches],
        }

    text = _json.dumps(bundle, indent=2, default=str)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        console.print(
            f"[green]Exported[/green] run {run_id} → {out_path} "
            f"({len(spans)} spans, {len(branches)} branches)"
        )
    else:
        click.echo(text)


@main.command()
@click.argument("run_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def delete(run_id: str, yes: bool) -> None:
    """Delete a single run and everything attached to it.

    The blunt alternative is `husk-studio clean`, which wipes every run you have. Use
    this to drop one noisy run and keep the rest.

    Replays forked from this run are kept; they just lose their parent pointer.
    """
    import shutil

    from husk_studio_backend.config import runs_dir
    from husk_studio_backend.db.engine import sync_engine, sync_session
    from husk_studio_backend.db.models import Base, RunRow, SpanRow

    Base.metadata.create_all(sync_engine())
    with sync_session() as s:
        run = s.get(RunRow, run_id)
        if run is None:
            console.print(f"[red]Run not found:[/red] {run_id}")
            sys.exit(1)
        n_spans = s.query(SpanRow).filter(SpanRow.run_id == run_id).count()
        label = run.script_path or run.framework
        if not yes:
            click.confirm(
                f"Delete run {run_id} ({label}, {n_spans} spans)? This is irreversible.",
                abort=True,
            )
        # Keep the children, drop the dangling parent pointer.
        for child in s.query(RunRow).filter(RunRow.parent_run_id == run_id).all():
            child.parent_run_id = None
            child.fork_span_id = None
        s.delete(run)
        s.commit()

    shutil.rmtree(runs_dir() / run_id, ignore_errors=True)
    console.print(f"[green]Deleted[/green] run {run_id} ({n_spans} spans)")


@main.group(invoke_without_command=True)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "sse"], case_sensitive=False),
    default="stdio",
    help="Transport: stdio for local clients (Claude Code, Cursor, …); http/sse for remote.",
)
@click.option("--host", default="127.0.0.1", help="Bind host for http/sse (default 127.0.0.1).")
@click.option("--port", default=7655, type=int, help="Bind port for http/sse (default 7655).")
@click.option(
    "--enable-replay",
    is_flag=True,
    help="Expose the replay tool. It executes your agent code — local use only. "
    "Equivalent to HUSK_MCP_ENABLE_REPLAY=1.",
)
@click.pass_context
def mcp(
    ctx: click.Context, transport: str, host: str, port: int, enable_replay: bool
) -> None:
    """Run the Husk MCP server (`husk-studio mcp`), or connect a client (`husk-studio mcp install`).

    Exposes runs, traces, costs, and replay to MCP clients like Claude Code,
    Cursor, Claude Desktop, Windsurf, and Lovable.
    """
    if ctx.invoked_subcommand is not None:
        return  # a subcommand (e.g. `install`) was given — don't start the server.

    from husk_studio.mcp.server import serve

    try:
        serve(
            transport=transport.lower(),
            host=host,
            port=port,
            enable_replay=enable_replay,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Husk Studio MCP server stopped.[/dim]")
        sys.exit(0)


@mcp.command(name="install")
@click.option(
    "--client",
    required=True,
    type=click.Choice(
        ["claude-code", "cursor", "claude-desktop", "windsurf", "lovable"],
        case_sensitive=False,
    ),
    help="Which client to configure.",
)
@click.option(
    "--scope",
    type=click.Choice(["user", "project"], case_sensitive=False),
    default="user",
    help="Config scope (applies to cursor and claude-code).",
)
@click.option(
    "--target-dir",
    default=None,
    help="Project directory for --scope project (default: current directory).",
)
@click.option("--force", is_flag=True, help="Overwrite an existing 'husk' entry.")
def mcp_install(
    client: str, scope: str, target_dir: str | None, force: bool
) -> None:
    """Write or print the MCP config needed to connect a client to Husk."""
    from husk_studio.mcp.install import install

    msg = install(
        client=client.lower(), scope=scope.lower(), target_dir=target_dir, force=force
    )
    console.print(msg)
    if msg.startswith("error:"):
        sys.exit(1)


@main.command()
def clean() -> None:
    """Remove the local Husk database and runs directory."""
    home = husk_home()
    db = db_path()
    if not click.confirm(f"Delete Husk Studio data under {home}? This is irreversible."):
        return
    import shutil

    if db.exists():
        db.unlink()
    runs = home / "runs"
    if runs.exists():
        shutil.rmtree(runs)
    click.echo("Cleaned.")


if __name__ == "__main__":
    main()
