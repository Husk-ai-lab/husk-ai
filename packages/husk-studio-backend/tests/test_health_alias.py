"""`/health` is the same JSON as `/api/health`, not the Studio's SPA fallback.

The desktop shell and any supervisor probe `/health`. It was never registered, so the catch-all
`/{full_path:path}` answered it with index.html (200 text/html) and a running backend could not be
told from any other web server. The route has to sit before that fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_health_is_json_and_equals_api_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    from httpx import ASGITransport, AsyncClient

    from husk_studio_backend.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        api = await client.get("/api/health")
        alias = await client.get("/health")

    assert alias.status_code == 200
    assert alias.headers["content-type"].startswith("application/json"), alias.text[:120]
    assert alias.json() == api.json()
    assert alias.json()["ok"] is True
