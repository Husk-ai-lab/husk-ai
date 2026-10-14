"""End-to-end tests for the PyInstaller-built `husk-studio` binary.

These boot the real executable (not the Python package) and drive it the way a user
would: the CLI, the HTTP server, OTLP ingest, the WebSocket, replay, and the MCP server.
They exist to catch what freezing breaks and unit tests cannot see: a missing hidden
import, missing package metadata, a Studio bundle that did not get baked in.

    uv run python packaging/build.py
    uv run python -m pytest packaging/tests -q

The binary is looked up at $HUSK_STUDIO_BINARY, then dist/husk-studio/husk-studio
(onedir), then dist/onefile/husk-studio (onefile). Without one the whole module is skipped, so a
plain `pytest -q` never needs a build.

Every process runs with a throwaway HUSK_HOME, from a cwd outside the repo, and only
ever talks to 127.0.0.1 (ingest refuses anything else by design).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.binary

REPO = Path(__file__).resolve().parents[2]
EXE = ".exe" if os.name == "nt" else ""


def _find_binary() -> Path | None:
    candidates = []
    if os.environ.get("HUSK_STUDIO_BINARY"):
        candidates.append(Path(os.environ["HUSK_STUDIO_BINARY"]))
    candidates += [
        REPO / "dist" / "husk-studio" / f"husk-studio{EXE}",
        REPO / "dist" / "onefile" / f"husk-studio{EXE}",
    ]
    return next((c for c in candidates if c.is_file()), None)


BINARY = _find_binary()
if BINARY is None:
    pytest.skip(
        "no husk-studio binary found (run `uv run python packaging/build.py`)",
        allow_module_level=True,
    )

EXPECTED_VERSION: str = tomllib.loads(
    (REPO / "packages" / "husk-studio-cli" / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]


# --------------------------------------------------------------------------- helpers


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    # A developer shell may be inside the repo's venv or have these set; the binary
    # must stand on its own.
    for k in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        env.pop(k, None)
    env.update(
        HUSK_HOME=str(home),
        HUSK_NO_BROWSER="1",
        HUSK_NO_AUTO_BUILD="1",
        PYTHONIOENCODING="utf-8",
        NO_COLOR="1",
        COLUMNS="200",
    )
    env.update(extra)
    return env


def run_cli(
    *args: str, home: Path, cwd: Path, timeout: float = 90, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    assert BINARY is not None
    return subprocess.run(
        [str(BINARY), *args],
        cwd=cwd,
        env=_env(home, **extra_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def otlp_json(trace_id: str, *, graph_module: str | None = None) -> dict:
    """A minimal 2-span GenAI trace in OTLP/JSON (agent.run -> chat gpt-4o)."""
    # Span ids are primary keys: derive them from the trace id so traces never collide.
    root_id = hashlib.sha1(f"{trace_id}:root".encode()).hexdigest()[:16]
    child_id = hashlib.sha1(f"{trace_id}:child".encode()).hexdigest()[:16]
    root_attrs = [{"key": "gen_ai.system", "value": {"stringValue": "openai"}}]
    if graph_module:
        root_attrs.append({"key": "husk.graph_module", "value": {"stringValue": graph_module}})
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "binary-test"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": root_id,
                                "name": "agent.run",
                                "startTimeUnixNano": "1000000",
                                "endTimeUnixNano": "2000000",
                                "attributes": root_attrs,
                                "status": {"code": 1},
                            },
                            {
                                "traceId": trace_id,
                                "spanId": child_id,
                                "parentSpanId": root_id,
                                "name": "chat gpt-4o",
                                "startTimeUnixNano": "1100000",
                                "endTimeUnixNano": "1900000",
                                "attributes": [
                                    {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                                    {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                                    {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
                                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "40"}},
                                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "12"}},
                                ],
                                "status": {"code": 1},
                            },
                        ]
                    }
                ],
            }
        ]
    }


def run_id_for(trace_id: str) -> str:
    """The backend derives a run id from the first 26 chars of the trace id."""
    return trace_id[:26]


# --------------------------------------------------------------------------- CLI (no server)


@pytest.fixture()
def cli_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture()
def cwd(tmp_path: Path) -> Path:
    d = tmp_path / "cwd"
    d.mkdir()
    return d


def test_version_matches_pyproject(cli_home: Path, cwd: Path) -> None:
    """Proves the package metadata was baked in (else it reports 0.0.0+unknown)."""
    r = run_cli("--version", home=cli_home, cwd=cwd)
    assert r.returncode == 0, r.stderr
    assert f"husk-studio, version {EXPECTED_VERSION}" in r.stdout
    assert "0.0.0+unknown" not in r.stdout


def test_help_lists_every_command(cli_home: Path, cwd: Path) -> None:
    r = run_cli("--help", home=cli_home, cwd=cwd)
    assert r.returncode == 0, r.stderr
    for cmd in ("start", "list", "doctor", "demo", "run", "replay", "export", "delete", "mcp", "clean"):
        assert re.search(rf"^\s+{cmd}\b", r.stdout, re.M), f"{cmd!r} missing from --help:\n{r.stdout}"


def test_doctor_reports_a_healthy_install(cli_home: Path, cwd: Path) -> None:
    r = run_cli("doctor", home=cli_home, cwd=cwd)
    assert r.returncode == 0, r.stderr
    out = " ".join(r.stdout.split())  # rich may wrap long paths
    assert f"husk-studio: {EXPECTED_VERSION}" in out
    assert str(cli_home).replace("\n", "") in out.replace(" ", "")
    assert "mcp: ready" in out, "the mcp package was not bundled"
    assert "replay: disabled (read-only)" in out


def test_list_on_a_fresh_home_is_empty_not_a_traceback(cli_home: Path, cwd: Path) -> None:
    r = run_cli("list", home=cli_home, cwd=cwd)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "runs (0)" in r.stdout


def test_export_unknown_run_exits_nonzero(cli_home: Path, cwd: Path) -> None:
    r = run_cli("export", "does-not-exist", home=cli_home, cwd=cwd)
    assert r.returncode == 1
    assert "Run not found" in r.stdout


def test_mcp_install_writes_a_command_that_points_at_the_binary(
    cli_home: Path, cwd: Path, tmp_path: Path
) -> None:
    """Frozen, the MCP client config must launch this binary, not a bare `husk-studio`."""
    target = tmp_path / "project"
    target.mkdir()
    r = run_cli(
        "mcp", "install", "--client", "claude-code", "--scope", "project",
        "--target-dir", str(target), home=cli_home, cwd=cwd,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    cfg = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    block = cfg["mcpServers"]["husk"]
    assert block["args"] == ["mcp"]
    assert BINARY is not None
    assert Path(block["command"]).resolve() == BINARY.resolve()


def test_run_executes_the_command_and_points_it_at_the_backend(
    cli_home: Path, cwd: Path
) -> None:
    r = run_cli(
        "run", "--no-serve", "--port", str(_free_port()), "--", sys.executable, "-c",
        "import os; print('ENDPOINT=' + os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'])",
        home=cli_home, cwd=cwd,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"ENDPOINT=http://127\.0\.0\.1:\d+", r.stdout)
    assert "View runs:" in r.stdout


def test_explicit_busy_port_fails_instead_of_drifting(cli_home: Path, cwd: Path) -> None:
    """`start --port N` means N. A supervisor told 7654 must not find the app on 7657."""
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        r = run_cli("start", "--port", str(port), "--no-open-browser", home=cli_home, cwd=cwd, timeout=30)
    assert r.returncode != 0, r.stdout + r.stderr
    assert f"port {port} is already in use" in r.stderr
    assert "Traceback" not in r.stderr
    assert not (cli_home / "port").exists(), "a port file for a server that never started"


def test_sigterm_stops_the_server_and_leaves_no_orphans(cli_home: Path, cwd: Path) -> None:
    """The shell stops Studio with SIGTERM, 5 s grace, then kill. On Windows
    `terminate()` is a hard TerminateProcess, so there only the process tree is asserted."""
    psutil = pytest.importorskip("psutil")
    assert BINARY is not None
    port = _free_port()
    log = cwd / "sigterm.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [str(BINARY), "start", "--port", str(port), "--host", "127.0.0.1", "--no-open-browser"],
            cwd=cwd,
            env=_env(cli_home),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            assert proc.poll() is None, f"exited early ({proc.returncode}):\n{log.read_text()[-2000:]}"
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            pytest.fail(f"never became healthy:\n{log.read_text()[-2000:]}")

        # onefile has a bootloader parent and a worker child; onedir has none. Either way
        # nothing in the tree may outlive the process the shell signalled.
        tree = psutil.Process(proc.pid).children(recursive=True)
        started = time.time()
        proc.terminate()
        code = proc.wait(timeout=10)
        stopped_in = time.time() - started
    finally:
        if proc.poll() is None:
            proc.kill()

    assert stopped_in < 5, f"took {stopped_in:.1f}s to stop; the shell kills after 5 s"
    if os.name != "nt":
        assert code in (0, -15), f"unexpected exit code {code}:\n{log.read_text()[-2000:]}"
    _, alive = psutil.wait_procs(tree, timeout=5)
    assert not alive, f"orphaned processes after SIGTERM: {[p.pid for p in alive]}"


@pytest.mark.skipif(os.name != "posix", reason="TIME_WAIT blocks a plain bind on POSIX only")
def test_restart_right_after_a_stop_binds_the_same_explicit_port(cli_home: Path, cwd: Path) -> None:
    """The shell stops Studio and starts it again on the same `--port` within seconds. A client
    that is still connected leaves TIME_WAIT sockets on that port for 30-60 s; they must not make
    `start --port N` exit 1 with "already in use"."""
    assert BINARY is not None
    port = _free_port()
    cmd = [str(BINARY), "start", "--port", str(port), "--host", "127.0.0.1", "--no-open-browser"]
    base = f"http://127.0.0.1:{port}"

    def boot(log: Path) -> subprocess.Popen[bytes]:
        with log.open("wb") as fh:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=_env(cli_home), stdout=fh, stderr=subprocess.STDOUT
            )
        deadline = time.time() + 90
        while time.time() < deadline:
            assert proc.poll() is None, f"exited early ({proc.returncode}):\n{log.read_text()[-2000:]}"
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    return proc
            except httpx.HTTPError:
                time.sleep(0.3)
        proc.kill()
        pytest.fail(f"never became healthy:\n{log.read_text()[-2000:]}")

    first = boot(cwd / "first.log")
    try:
        with httpx.Client(base_url=base) as client:  # keep-alive connection stays open
            assert client.get("/api/health").status_code == 200
            first.terminate()
            assert first.wait(timeout=10) in (0, -15)
    finally:
        if first.poll() is None:
            first.kill()

    second = boot(cwd / "second.log")  # immediately, no wait for TIME_WAIT to drain
    try:
        assert httpx.get(f"{base}/api/health", timeout=5).json()["ok"] is True
    finally:
        second.terminate()
        try:
            second.wait(timeout=10)
        except subprocess.TimeoutExpired:
            second.kill()


def test_mcp_server_speaks_stdio_and_lists_its_tools(cli_home: Path, cwd: Path) -> None:
    """Proves `mcp` (and its `version("mcp")` metadata lookup) survived freezing."""
    assert BINARY is not None
    proc = subprocess.Popen(
        [str(BINARY), "mcp"],
        cwd=cwd,
        env=_env(cli_home),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    lines: queue.Queue[str] = queue.Queue()
    assert proc.stdout is not None and proc.stdin is not None

    def pump() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def recv(want_id: int, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = json.loads(lines.get(timeout=0.5))
            except (queue.Empty, json.JSONDecodeError):
                continue
            if msg.get("id") == want_id:
                return msg
        raise AssertionError(f"no response to request {want_id}")

    try:
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "binary-test", "version": "0"},
            },
        })  # fmt: skip
        assert "result" in recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in recv(2)["result"]["tools"]}
        assert {"list_runs", "get_trace", "cost_breakdown"} <= names
        assert "replay_run" not in names, "replay must stay off unless explicitly enabled"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- server


@dataclass
class Server:
    url: str
    port: int
    home: Path
    cwd: Path
    graph_dir: Path
    log: Path

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, timeout=30)

    def logs(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")[-4000:]


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    assert BINARY is not None
    base = tmp_path_factory.mktemp("server")
    home, cwd, graph_dir = base / "home", base / "cwd", base / "graphs"
    for d in (home, cwd, graph_dir):
        d.mkdir()
    port = _free_port()
    log = base / "server.log"
    with log.open("wb") as fh:
        proc = subprocess.Popen(
            [str(BINARY), "start", "--port", str(port), "--no-open-browser"],
            cwd=cwd,
            env=_env(home, HUSK_ALLOWED_GRAPH_DIRS=str(graph_dir)),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
        srv = Server(f"http://127.0.0.1:{port}", port, home, cwd, graph_dir, log)
        try:
            deadline = time.time() + 90  # onefile cold start extracts first
            while time.time() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"server exited early ({proc.returncode}):\n{srv.logs()}")
                try:
                    if httpx.get(f"{srv.url}/api/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.3)
            else:
                pytest.fail(f"server did not become healthy:\n{srv.logs()}")
            yield srv
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def _ingest(srv: Server, trace_id: str, **kw: str) -> None:
    with srv.client() as c:
        r = c.post("/v1/traces", json=otlp_json(trace_id, **kw))
        assert r.status_code == 200, r.text


def _wait_for(fn, what: str, timeout: float = 20):  # noqa: ANN001
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.4)
    raise AssertionError(f"timed out waiting for {what} (last={last!r})")


def test_health_reports_the_backend_version(server: Server) -> None:
    with server.client() as c:
        body = c.get("/api/health").json()
        assert body["ok"] is True
        assert body["service"] == "husk-studio-backend"
        assert body["version"] == EXPECTED_VERSION
        assert c.get("/healthz").json() == {"ok": True}


def test_health_root_is_json_not_the_ui_fallback(server: Server) -> None:
    """The desktop shell probes /health. Unregistered, the SPA fallback answers it with
    index.html (200 text/html), so a dead backend and a live one look the same."""
    with server.client() as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json"), r.text[:200]
        assert r.json() == c.get("/api/health").json()
        assert r.json()["version"] == EXPECTED_VERSION


def test_port_file_is_written(server: Server) -> None:
    assert (server.home / "port").read_text().strip() == str(server.port)


def test_studio_ui_is_baked_into_the_binary(server: Server) -> None:
    with server.client() as c:
        page = c.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert 'id="root"' in page.text
        assert "Studio not built yet" not in page.text, "served the fallback page: no bundle"
        assert "Husk Studio" in page.text

        script = re.search(r'src="(/assets/[^"]+\.js)"', page.text)
        assert script, page.text
        assert c.get(script.group(1)).status_code == 200
        assert c.get("/favicon.png").status_code == 200
        # Client-side routes fall back to the SPA shell.
        runs = c.get("/runs")
        assert runs.status_code == 200 and 'id="root"' in runs.text


def test_studio_ships_the_husk_studio_palette(server: Server) -> None:
    """The recolor is in the compiled CSS: violet #7456f4 (light) / #8c71ff (dark)."""
    with server.client() as c:
        page = c.get("/").text
        css = re.search(r'href="(/assets/[^"]+\.css)"', page)
        assert css, page
        body = c.get(css.group(1)).text
        assert "oklch(57.5% .225 286)" in body, "light accent #7456f4 missing"
        assert "oklch(64.5% .202 288)" in body, "dark accent #8c71ff missing"


def test_unknown_api_route_is_json_404_not_the_ui(server: Server) -> None:
    with server.client() as c:
        r = c.get("/api/definitely-not-a-route")
        assert r.status_code == 404
        assert "text/html" not in r.headers.get("content-type", "")


def test_empty_database_endpoints(server: Server) -> None:
    with server.client() as c:
        summary = c.get("/api/dashboard/summary").json()
        assert {"totals", "recent_runs", "sparkline"} <= set(summary)
        assert c.get("/api/integrations/status").status_code == 200


def test_otlp_json_ingest_round_trips_through_the_database(server: Server) -> None:
    trace_id = "11112222333344445555666677778888"
    run_id = run_id_for(trace_id)
    _ingest(server, trace_id)

    with server.client() as c:
        runs = c.get("/api/v1/runs").json()
        assert any(r["id"] == run_id for r in runs)

        run = c.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "success"
        assert run["total_tokens_in"] == 40 and run["total_tokens_out"] == 12

        spans = c.get(f"/api/v1/runs/{run_id}/spans").json()
        chat = next(s for s in spans if s.get("model") == "gpt-4o")
        assert chat["tokens_in"] == 40 and chat["tokens_out"] == 12

        assert c.get(f"/api/v1/runs/{run_id}/breakdown").status_code == 200
        assert c.get(f"/api/v1/runs/{run_id}/graph").status_code == 200


def test_otlp_protobuf_ingest(server: Server) -> None:
    """Needs google.protobuf + opentelemetry.proto frozen into the bundle."""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    trace_id = "9999aaaabbbbccccddddeeeeffff0000"
    req = ExportTraceServiceRequest()
    span = req.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex(trace_id)
    span.span_id = bytes.fromhex("1234567812345678")
    span.name = "proto.root"
    span.start_time_unix_nano = 1_000_000
    span.end_time_unix_nano = 2_000_000

    with server.client() as c:
        ok = c.post(
            "/v1/traces",
            content=req.SerializeToString(),
            headers={"content-type": "application/x-protobuf"},
        )
        assert ok.status_code == 200, ok.text
        assert any(r["id"] == run_id_for(trace_id) for r in c.get("/api/v1/runs").json())

        bad = c.post(
            "/v1/traces", content=b"\xff\xfe not a protobuf", headers={"content-type": "application/x-protobuf"}
        )
        assert bad.status_code == 400
        unsupported = c.post("/v1/traces", content=b"x", headers={"content-type": "text/plain"})
        assert unsupported.status_code == 415


def test_websocket_replays_the_run_backlog(server: Server) -> None:
    """Needs uvicorn's websockets implementation frozen into the bundle."""
    from websockets.sync.client import connect

    trace_id = "ab12ab12ab12ab12ab12ab12ab12ab12"
    _ingest(server, trace_id)
    ws_url = f"ws://127.0.0.1:{server.port}/ws/runs/{run_id_for(trace_id)}"
    with connect(ws_url, open_timeout=15) as ws:
        first = json.loads(ws.recv(timeout=15))
    assert first["type"] == "span.replay.batch"
    assert {s["name"] for s in first["spans"]} >= {"agent.run", "chat gpt-4o"}


def test_demo_seeds_a_run_via_the_otel_exporter(server: Server, cwd: Path) -> None:
    """The demo uses the OTel SDK + OTLP/HTTP exporter + requests inside the bundle,
    and finds the server through the port file (no --url)."""
    r = run_cli("demo", home=server.home, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Demo data ready" in r.stdout

    def demo_run() -> dict | None:
        with server.client() as c:
            runs = c.get("/api/v1/runs").json()
        return next((x for x in runs if x.get("script_path") == "husk-ai-demo"), None)

    run = _wait_for(demo_run, "the demo run to arrive")
    assert run["framework"] == "otel/openai"
    with server.client() as c:
        spans = c.get(f"/api/v1/runs/{run['id']}/spans").json()
    assert 3 <= len(spans) <= 4


def test_replay_reruns_a_graph_inside_the_frozen_interpreter(server: Server) -> None:
    """Modify-and-replay executes the user's graph in the bundled interpreter; the
    bundled example only needs opentelemetry + husk_studio_shared, both frozen in."""
    graph = server.graph_dir / "husk_thread.py"
    shutil.copy(REPO / "examples" / "husk_thread.py", graph)
    trace_id = "c0ffee00c0ffee00c0ffee00c0ffee00"
    _ingest(server, trace_id, graph_module=f"{graph}:agent")

    with server.client() as c:
        r = c.post(
            "/api/replay",
            json={"run_id": run_id_for(trace_id), "state_override": {"topic": "Tokyo"}},
            timeout=60,
        )
    assert r.status_code == 200, r.text + server.logs()
    state = r.json()["state"]
    assert state["topic"] == "Tokyo"
    assert "Tokyo" in state["answer"]


def test_replay_refuses_a_graph_outside_the_allowed_roots(
    server: Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "evil.py"
    outside.write_text("graph = object()\n", encoding="utf-8")
    trace_id = "dead0000dead0000dead0000dead0000"
    _ingest(server, trace_id, graph_module=f"{outside}:graph")
    with server.client() as c:
        r = c.post("/api/replay", json={"run_id": run_id_for(trace_id), "state_override": {}})
    assert r.status_code == 403
    assert "allowed roots" in r.text


def test_export_then_delete_round_trip(server: Server, tmp_path: Path, cwd: Path) -> None:
    trace_id = "5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e"
    run_id = run_id_for(trace_id)
    _ingest(server, trace_id)

    out = tmp_path / "bundle.json"
    r = run_cli("export", run_id, "--out", str(out), home=server.home, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    bundle = json.loads(out.read_text(encoding="utf-8"))
    assert bundle["husk_export_version"] == 1
    assert bundle["husk_version"] == EXPECTED_VERSION
    assert bundle["run"]["id"] == run_id
    assert len(bundle["spans"]) == 2

    r = run_cli("delete", run_id, "--yes", home=server.home, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr
    with server.client() as c:
        assert c.get(f"/api/v1/runs/{run_id}").status_code == 404
