from __future__ import annotations

import logging
import os
import socket
import threading

import uvicorn

from husk_studio.ui_launcher import open_browser_at

log = logging.getLogger(__name__)


def _listener_answers(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("", "0.0.0.0") else host
    try:
        with socket.create_connection((probe, port), timeout=0.25):
            return True
    except OSError:
        return False


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if os.name == "posix":
            # Bind the way uvicorn does: sockets in TIME_WAIT from a run that just stopped
            # (30-60 s) must not make a free port look busy. A live listener on the same
            # address still fails the bind. Not set on Windows, where SO_REUSEADDR would let
            # two sockets share a port and report a live listener as free.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    # BSD/macOS let a specific bind coexist with a wildcard listener once SO_REUSEADDR is
    # set, so also ask whether anything answers there.
    return not (os.name == "posix" and _listener_answers(host, port))


class PortInUseError(RuntimeError):
    """An explicitly requested port is busy and must not be silently replaced."""


def _resolve_port(host: str, port: int, *, strict: bool = False) -> int:
    """If `port` is busy, scan a small window for the next free port.

    With `strict=True` (the caller asked for this exact port) a busy port is an
    error instead: a supervisor that was told 7654 must not find us on 7657.
    """
    if _port_free(host, port):
        return port
    if strict:
        raise PortInUseError(f"port {port} is already in use")
    for p in range(port + 1, port + 11):
        if _port_free(host, p):
            log.warning("port %d busy; using %d instead", port, p)
            return p
    raise RuntimeError(
        f"Could not find a free port in {port}..{port + 10}. "
        f"Stop the conflicting process or pass --port."
    )


def _write_port_file(port: int) -> None:
    """Record the bound port so client commands (`demo`, `replay`) find the
    backend even when `start` auto-bumped off a busy 7654. Best-effort."""
    try:
        from husk_studio.config import husk_home

        (husk_home() / "port").write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def start_server(
    *,
    host: str = "127.0.0.1",
    port: int = 7654,
    open_browser: bool = True,
    strict_port: bool = False,
) -> None:
    """Boot the Husk FastAPI backend in the foreground.

    Blocks until Ctrl+C. The browser is opened ~1.2s after boot so uvicorn
    has time to bind the socket before the user lands. With `strict_port`, a
    busy `port` raises PortInUseError instead of moving to the next free one.
    """
    port = _resolve_port(host, port, strict=strict_port)
    _write_port_file(port)

    if open_browser:
        url = f"http://localhost:{port}"
        threading.Timer(1.2, lambda: open_browser_at(url)).start()

    log.info("husk-studio starting on http://%s:%d", host, port)
    config = uvicorn.Config(
        "husk_studio_backend.main:app",
        host=host,
        port=port,
        log_level="info",
    )
    uvicorn.Server(config).run()
