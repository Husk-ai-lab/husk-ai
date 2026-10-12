"""HTTP cassettes: record real LLM/tool HTTP calls, replay them without a network.

This is what makes Husk's replay *model-free*. During an original run we record
every provider HTTP request keyed by a stable hash of (method, url, body). During
a replay we look the request up and return the recorded response — no API call,
no token cost, byte-identical output. A request that *changed* (because you
edited upstream state or code) is a cache MISS and falls through to the real
provider, then is recorded too. So:

  * change nothing            -> 100% cache hits -> free, deterministic replay
  * change non-LLM code/state -> only the affected calls miss and re-run for real
  * change upstream LLM input  -> downstream requests differ -> they miss (real)

The interception point is the httpx transport boundary, which the OpenAI,
Anthropic, and Groq SDKs all ride on, so one mechanism covers every provider.

Limitations (documented, not hidden): streaming/SSE responses are recorded as
their fully-read body; non-deterministic *non-HTTP* effects (wall-clock, RNG,
local file/db writes) are out of scope.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Headers that are volatile or recomputed by httpx — never stored/replayed.
_DROP_RESPONSE_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


def request_key(method: str, url: str, body: bytes | None) -> str:
    """Stable sha256 over method + url + canonicalized body.

    The body is canonicalized as sorted-key JSON when it parses as JSON (so
    semantically identical requests collide regardless of key order); otherwise
    the raw bytes are hashed. Headers (auth, date, request-id) are deliberately
    excluded — they are volatile and must not affect the match.
    """
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\n")
    h.update(url.encode())
    h.update(b"\n")
    if body:
        try:
            canon = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))
            h.update(canon.encode())
        except (ValueError, TypeError):
            h.update(body)
    return h.hexdigest()


class CassetteStore:
    """A directory of recorded HTTP exchanges, one JSON file per request hash."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        entry: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return entry

    def put(
        self,
        key: str,
        *,
        method: str,
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        latency_ms: int = 0,
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": key,
            "method": method.upper(),
            "url": url,
            "status": status,
            "headers": {
                k: v for k, v in headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS
            },
            "body_b64": base64.b64encode(body).decode("ascii"),
            "latency_ms": latency_ms,
        }
        # Atomic-ish write so a concurrent reader never sees a half file.
        tmp = self._path(key).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        tmp.replace(self._path(key))

    def keys(self) -> list[str]:
        if not self.dir.exists():
            return []
        return [p.stem for p in self.dir.glob("*.json")]

    def __len__(self) -> int:
        return len(self.keys())


def _response_from_entry(entry: dict[str, Any], request: httpx.Request) -> httpx.Response:
    body = base64.b64decode(entry["body_b64"])
    return httpx.Response(
        status_code=int(entry["status"]),
        headers=entry.get("headers", {}),
        content=body,
        request=request,
    )


def _record_response(store: CassetteStore, request: httpx.Request, response: httpx.Response) -> httpx.Response:
    """Read `response`, persist it under the request key, return a fresh Response."""
    body = response.read()
    store.put(
        request_key(request.method, str(request.url), request.content),
        method=request.method,
        url=str(request.url),
        status=response.status_code,
        headers=dict(response.headers),
        body=body,
    )
    return httpx.Response(
        status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS},
        content=body,
        request=request,
    )


class CassetteMissError(RuntimeError):
    """A replay in strict mode hit a request with no recorded response."""


class RecordingTransport(httpx.BaseTransport):
    """Wraps a real sync transport, recording every exchange to the store."""

    def __init__(self, store: CassetteStore, inner: httpx.BaseTransport | None = None) -> None:
        self._store = store
        self._inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return _record_response(self._store, request, self._inner.handle_request(request))


class ReplayingTransport(httpx.BaseTransport):
    """Serve requests from the store; fall through to the real transport on a miss.

    With ``allow_passthrough=False`` a miss raises CassetteMissError instead —
    used by tests to prove that a replay makes *zero* live calls.
    """

    def __init__(
        self,
        store: CassetteStore,
        inner: httpx.BaseTransport | None = None,
        *,
        allow_passthrough: bool = True,
    ) -> None:
        self._store = store
        self._inner = inner or httpx.HTTPTransport()
        self._allow_passthrough = allow_passthrough
        self.hits = 0
        self.misses = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = request_key(request.method, str(request.url), request.content)
        entry = self._store.get(key)
        if entry is not None:
            self.hits += 1
            return _response_from_entry(entry, request)
        self.misses += 1
        if not self._allow_passthrough:
            raise CassetteMissError(f"no cassette for {request.method} {request.url}")
        return _record_response(self._store, request, self._inner.handle_request(request))


# ── generic monkeypatch installers (for SDKs that build their own httpx client) ──

_PATCH_ATTR = "_husk_orig_handle_request"


def install_record_mode(cassette_dir: str | Path) -> CassetteStore:
    """Patch httpx so *all* sync requests are recorded to ``cassette_dir``."""
    store = CassetteStore(cassette_dir)
    if getattr(httpx.HTTPTransport, _PATCH_ATTR, None) is None:
        orig = httpx.HTTPTransport.handle_request

        def patched(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            return _record_response(store, request, orig(self, request))

        setattr(httpx.HTTPTransport, _PATCH_ATTR, orig)
        httpx.HTTPTransport.handle_request = patched  # type: ignore[method-assign]
    return store


def install_replay_mode(cassette_dir: str | Path, *, allow_passthrough: bool = True) -> CassetteStore:
    """Patch httpx so sync requests are served from ``cassette_dir`` (miss -> real)."""
    store = CassetteStore(cassette_dir)
    if getattr(httpx.HTTPTransport, _PATCH_ATTR, None) is None:
        orig = httpx.HTTPTransport.handle_request

        def patched(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
            key = request_key(request.method, str(request.url), request.content)
            entry = store.get(key)
            if entry is not None:
                return _response_from_entry(entry, request)
            if not allow_passthrough:
                raise CassetteMissError(f"no cassette for {request.method} {request.url}")
            return _record_response(store, request, orig(self, request))

        setattr(httpx.HTTPTransport, _PATCH_ATTR, orig)
        httpx.HTTPTransport.handle_request = patched  # type: ignore[method-assign]
    return store


def uninstall() -> None:
    """Undo install_record_mode/install_replay_mode (restore httpx)."""
    orig = getattr(httpx.HTTPTransport, _PATCH_ATTR, None)
    if orig is not None:
        httpx.HTTPTransport.handle_request = orig  # type: ignore[method-assign]
        delattr(httpx.HTTPTransport, _PATCH_ATTR)


@contextmanager
def recording(cassette_dir: str | Path) -> Iterator[CassetteStore]:
    try:
        yield install_record_mode(cassette_dir)
    finally:
        uninstall()


@contextmanager
def replaying(cassette_dir: str | Path, *, allow_passthrough: bool = True) -> Iterator[CassetteStore]:
    try:
        yield install_replay_mode(cassette_dir, allow_passthrough=allow_passthrough)
    finally:
        uninstall()
