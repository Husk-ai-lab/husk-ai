"""`husk-studio start --port N` means exactly N; without `--port` the 7654..7664 fallback stays.

A supervisor (the desktop shell) that passes `--port 7654` and finds the app on 7657 never sees it
come up, so an explicit port that is busy has to be an error, not a quiet move.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any

import pytest
from click.testing import CliRunner

from husk_studio import server
from husk_studio.cli import main


def _busy_port() -> tuple[socket.socket, int]:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, int(s.getsockname()[1])


def test_resolve_port_strict_raises_instead_of_moving() -> None:
    busy, port = _busy_port()
    try:
        with pytest.raises(server.PortInUseError, match=f"port {port} is already in use"):
            server._resolve_port("127.0.0.1", port, strict=True)
        # the default keeps today's behaviour: scan the next ten ports
        assert server._resolve_port("127.0.0.1", port) != port
    finally:
        busy.close()


def test_resolve_port_strict_returns_a_free_port_unchanged() -> None:
    probe, port = _busy_port()
    probe.close()
    assert server._resolve_port("127.0.0.1", port, strict=True) == port


posix_only = pytest.mark.skipif(os.name != "posix", reason="TIME_WAIT/SO_REUSEADDR semantics are POSIX")


@posix_only
def test_a_port_with_only_time_wait_leftovers_is_free() -> None:
    """A run that just stopped leaves TIME_WAIT sockets on its port for 30-60 s. They are not a
    listener, and uvicorn binds over them, so `--port` must not treat them as busy."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = int(srv.getsockname()[1])
    cli = socket.create_connection(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()  # the server side closes first -> TIME_WAIT on `port`
    time.sleep(0.1)
    cli.close()
    srv.close()
    assert server._resolve_port("127.0.0.1", port, strict=True) == port


@posix_only
def test_a_live_listener_is_still_refused_with_so_reuseaddr_set() -> None:
    """uvicorn listens with SO_REUSEADDR; the check must not mistake that for free."""
    live = socket.socket()
    live.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    live.bind(("127.0.0.1", 0))
    live.listen()
    port = int(live.getsockname()[1])
    try:
        with pytest.raises(server.PortInUseError):
            server._resolve_port("127.0.0.1", port, strict=True)
        assert server._resolve_port("127.0.0.1", port) != port
    finally:
        live.close()


@posix_only
def test_a_wildcard_listener_is_refused_for_a_loopback_start() -> None:
    """macOS lets a loopback bind coexist with a 0.0.0.0 listener once SO_REUSEADDR is set
    (e.g. `docker run -p 7654:7654`); Studio would then shadow it. It must read as busy."""
    wild = socket.socket()
    wild.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    wild.bind(("0.0.0.0", 0))
    wild.listen()
    port = int(wild.getsockname()[1])
    try:
        with pytest.raises(server.PortInUseError):
            server._resolve_port("127.0.0.1", port, strict=True)
    finally:
        wild.close()


def test_start_with_a_busy_explicit_port_exits_nonzero_with_one_clear_line(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    busy, port = _busy_port()
    try:
        result = CliRunner().invoke(main, ["start", "--port", str(port), "--no-open-browser"])
    finally:
        busy.close()
    assert result.exit_code == 1
    assert f"port {port} is already in use" in result.output
    assert "--port was given explicitly" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "port").exists()


@pytest.mark.parametrize(
    ("argv", "strict"),
    [(["start", "--port", "7700"], True), (["start"], False)],
    ids=["explicit-port-is-strict", "default-port-may-drift"],
)
def test_start_passes_strictness_by_whether_port_was_given(
    argv: list[str], strict: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_start_server(**kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr(server, "start_server", fake_start_server)
    result = CliRunner().invoke(main, [*argv, "--no-open-browser"])
    assert result.exit_code == 0, result.output
    assert seen["strict_port"] is strict
    assert seen["open_browser"] is False
