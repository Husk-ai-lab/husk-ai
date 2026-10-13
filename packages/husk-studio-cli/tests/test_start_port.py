"""`husk-studio start --port N` means exactly N; without `--port` the 7654..7664 fallback stays.

A supervisor (the desktop shell) that passes `--port 7654` and finds the app on 7657 never sees it
come up, so an explicit port that is busy has to be an error, not a quiet move.
"""

from __future__ import annotations

import socket
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
