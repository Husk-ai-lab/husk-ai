"""HTTP cassette engine: stable keys + record once, replay with zero network.

This backs the "replay without re-calling the model" claim at the layer the LLM
SDKs ride on (httpx). The model-free guarantee is the strict-replay test:
after recording, an identical request is served from the cassette and the inner
(network) transport is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from husk_studio_sandbox.cassette import (
    CassetteMissError,
    CassetteStore,
    RecordingTransport,
    ReplayingTransport,
    install_record_mode,
    install_replay_mode,
    request_key,
    uninstall,
)

URL = "https://api.example.test/v1/chat/completions"


def _mock(calls: list[str], reply: dict | None = None):
    """An httpx MockTransport handler that records that it was called."""
    reply = reply or {"id": "cmpl-1", "choices": [{"message": {"content": "hi"}}]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(handler)


# ── request key ──────────────────────────────────────────────────────────────

def test_request_key_is_order_independent_for_json() -> None:
    a = request_key("POST", URL, json.dumps({"model": "m", "n": 1}).encode())
    b = request_key("POST", URL, json.dumps({"n": 1, "model": "m"}).encode())
    assert a == b


def test_request_key_changes_with_body_and_method() -> None:
    base = request_key("POST", URL, b'{"model":"m","msg":"a"}')
    assert base != request_key("POST", URL, b'{"model":"m","msg":"b"}')
    assert base != request_key("GET", URL, b'{"model":"m","msg":"a"}')


# ── record then replay ───────────────────────────────────────────────────────

def test_record_then_replay_is_model_free(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path / "cass")
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

    # 1) Record: the inner (network) transport is hit exactly once.
    rec_calls: list[str] = []
    rec = httpx.Client(transport=RecordingTransport(store, inner=_mock(rec_calls)))
    r1 = rec.post(URL, json=body)
    assert r1.status_code == 200 and r1.json()["choices"][0]["message"]["content"] == "hi"
    assert len(rec_calls) == 1
    assert len(store) == 1

    # 2) Replay (strict): identical request is served from the cassette with the
    #    inner transport guaranteed untouched — proves zero model calls.
    rep_calls: list[str] = []
    transport = ReplayingTransport(store, inner=_mock(rep_calls), allow_passthrough=False)
    rep = httpx.Client(transport=transport)
    r2 = rep.post(URL, json=body)
    assert r2.json() == r1.json()
    assert transport.hits == 1 and transport.misses == 0
    assert rep_calls == []  # the network was never touched


def test_replay_miss_passes_through_and_records(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path / "cass")
    seen: list[str] = []
    transport = ReplayingTransport(store, inner=_mock(seen), allow_passthrough=True)
    client = httpx.Client(transport=transport)

    # Empty cassette -> miss -> passthrough to the (mock) network + record.
    client.post(URL, json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert transport.misses == 1 and len(seen) == 1 and len(store) == 1

    # Same request again -> hit, no further network.
    client.post(URL, json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
    assert transport.hits == 1 and len(seen) == 1


def test_strict_replay_raises_on_unknown_request(tmp_path: Path) -> None:
    store = CassetteStore(tmp_path / "cass")
    transport = ReplayingTransport(store, inner=_mock([]), allow_passthrough=False)
    client = httpx.Client(transport=transport)
    with pytest.raises(CassetteMissError):
        client.post(URL, json={"model": "m", "messages": []})


def test_install_uninstall_restores_httpx() -> None:
    original = httpx.HTTPTransport.handle_request
    install_record_mode("/tmp/husk-cassette-noop")
    assert httpx.HTTPTransport.handle_request is not original
    uninstall()
    assert httpx.HTTPTransport.handle_request is original


def test_install_replay_mode_serves_default_client_without_network(tmp_path: Path) -> None:
    # Record one exchange through a mock, then replay via the global monkeypatch
    # using a plain httpx.Client() (default transport). With passthrough off, a
    # served response proves the default-transport call never reached the network.
    store = CassetteStore(tmp_path / "cass")
    body = {"model": "m", "messages": [{"role": "user", "content": "q"}]}
    httpx.Client(transport=RecordingTransport(store, inner=_mock([]))).post(URL, json=body)
    assert len(store) == 1

    install_replay_mode(tmp_path / "cass", allow_passthrough=False)
    try:
        resp = httpx.Client().post(URL, json=body)
        assert resp.json()["choices"][0]["message"]["content"] == "hi"
    finally:
        uninstall()
