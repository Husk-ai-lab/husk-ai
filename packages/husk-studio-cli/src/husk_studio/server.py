from __future__ import annotations

import logging
import socket
import threading

import uvicorn

from husk_studio.ui_launcher import open_browser_at

log = logging.getLogger(__name__)


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


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
