"""Tests for the husk-studio run / replay / export CLI commands (added with the features)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HUSK_HOME", str(tmp_path))
    return tmp_path


def test_run_injects_otel_env_and_runs_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import husk_studio.cli as cli

    # Pretend the backend is already up so `run` doesn't try to boot one.
    monkeypatch.setattr(cli, "_backend_healthy", lambda base: True)

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

    def _fake_run(cmd: list[str], env: dict[str, str] | None = None) -> _FakeProc:
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    res = CliRunner().invoke(cli.main, ["run", "--port", "7654", "echo", "hi"])
    assert res.exit_code == 0, res.output
    assert captured["cmd"] == ["echo", "hi"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:7654"
    assert "127.0.0.1:7654/runs" in res.output


def test_replay_posts_expected_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    import husk_studio.cli as cli

    captured: dict[str, object] = {}

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, str]:
            return {"thread_id": "t", "child_id": "c"}

    def _fake_post(url: str, json: dict | None = None, timeout: float | None = None) -> _FakeResp:
        captured["url"] = url
        captured["body"] = json
        return _FakeResp()

    monkeypatch.setattr(httpx, "post", _fake_post)

    res = CliRunner().invoke(
        cli.main,
        ["replay", "run1", "--set", "topic=Tokyo", "--set", "n=3", "--cassette"],
    )
    assert res.exit_code == 0, res.output
    assert str(captured["url"]).endswith("/api/replay")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["run_id"] == "run1"
    # "Tokyo" isn't valid JSON -> kept as string; "3" parses as int.
    assert body["state_override"] == {"topic": "Tokyo", "n": 3}
    assert body["use_cassette"] is True


def test_export_bundles_run_spans_branches(isolated_home: Path) -> None:
    from husk_studio_backend.db.engine import sync_engine, sync_session
    from husk_studio_backend.db.models import Base, RunRow, SpanRow

    # Create the schema on the (global) sync engine and seed one run + span.
    Base.metadata.create_all(sync_engine())
    with sync_session() as s:
        s.add(
            RunRow(
                id="exp_run_1",
                script_path="agent.py",
                framework="otel/openai",
                status="success",
                started_at=1,
                finished_at=2,
            )
        )
        s.add(
            SpanRow(
                id="exp_span_1",
                run_id="exp_run_1",
                kind="llm",
                name="chat",
                started_at=1,
                finished_at=2,
                status="success",
                input_inline={"messages": [{"role": "user", "content": "hi"}]},
            )
        )
        s.commit()

    import husk_studio.cli as cli

    res = CliRunner().invoke(cli.main, ["export", "exp_run_1"])
    assert res.exit_code == 0, res.output
    bundle = json.loads(res.output)
    assert bundle["husk_export_version"] == 1
    assert bundle["run"]["id"] == "exp_run_1"
    assert len(bundle["spans"]) == 1
    assert bundle["spans"][0]["id"] == "exp_span_1"
    assert "branches" in bundle


def test_export_unknown_run_errors() -> None:
    import husk_studio.cli as cli

    res = CliRunner().invoke(cli.main, ["export", "does-not-exist"])
    assert res.exit_code == 1
    assert "not found" in res.output.lower()


def _seed_run(run_id: str, *, parent: str | None = None) -> None:
    """Insert a run (plus one span) straight into the local DB."""
    from husk_studio_backend.db.engine import sync_engine, sync_session
    from husk_studio_backend.db.models import Base, RunRow, SpanRow

    Base.metadata.create_all(sync_engine())
    with sync_session() as s:
        s.add(
            RunRow(
                id=run_id,
                script_path="agent.py",
                framework="otel/test",
                status="success",
                started_at=1,
                parent_run_id=parent,
            )
        )
        s.add(
            SpanRow(
                id=f"span-{run_id}", run_id=run_id, kind="llm", name="call", started_at=1
            )
        )
        s.commit()


def test_delete_removes_the_run_and_its_spans(isolated_home: Path) -> None:
    """Before this command the only way to drop a run was `husk-studio clean`, which
    wipes the whole database."""
    import husk_studio.cli as cli
    from husk_studio_backend.db.engine import sync_session
    from husk_studio_backend.db.models import RunRow, SpanRow

    _seed_run("run-a")
    res = CliRunner().invoke(cli.main, ["delete", "run-a", "--yes"])
    assert res.exit_code == 0, res.output

    with sync_session() as s:
        assert s.get(RunRow, "run-a") is None
        assert s.query(SpanRow).filter(SpanRow.run_id == "run-a").count() == 0


def test_delete_keeps_replays_forked_from_the_deleted_run(isolated_home: Path) -> None:
    import husk_studio.cli as cli
    from husk_studio_backend.db.engine import sync_session
    from husk_studio_backend.db.models import RunRow

    _seed_run("parent-1")
    _seed_run("child-1", parent="parent-1")

    res = CliRunner().invoke(cli.main, ["delete", "parent-1", "--yes"])
    assert res.exit_code == 0, res.output

    with sync_session() as s:
        child = s.get(RunRow, "child-1")
        assert child is not None, "deleting a parent must not destroy its replays"
        assert child.parent_run_id is None


def test_delete_aborts_without_confirmation(isolated_home: Path) -> None:
    import husk_studio.cli as cli
    from husk_studio_backend.db.engine import sync_session
    from husk_studio_backend.db.models import RunRow

    _seed_run("run-b")
    res = CliRunner().invoke(cli.main, ["delete", "run-b"], input="n\n")
    assert res.exit_code != 0
    with sync_session() as s:
        assert s.get(RunRow, "run-b") is not None


def test_delete_unknown_run_exits_nonzero(isolated_home: Path) -> None:
    import husk_studio.cli as cli

    res = CliRunner().invoke(cli.main, ["delete", "nope", "--yes"])
    assert res.exit_code == 1
    assert "not found" in res.output.lower()


def test_export_survives_a_non_utf8_stdout(isolated_home: Path, tmp_path: Path) -> None:
    """`husk-studio export --out FILE` wrote the file and then died with
    UnicodeEncodeError on the success message, because a redirected stdout on
    Windows falls back to cp1252 and the message contains an arrow. The file was
    fine but the non-zero exit broke any script wrapping the command."""
    # The sync engine is cached module-wide, so it may still point at a previous
    # test's HUSK_HOME. Reset it so the seed lands in the home the subprocess reads.
    from husk_studio_backend.db import engine as _engine

    _engine._sync_engine = None
    _engine._sync_factory = None

    _seed_run("run-utf8")
    out = tmp_path / "bundle.json"
    env = {
        **os.environ,
        "HUSK_HOME": str(isolated_home),
        "PYTHONIOENCODING": "cp1252",  # reproduce the legacy Windows console
    }
    proc = subprocess.run(
        [sys.executable, "-m", "husk_studio", "export", "run-utf8", "--out", str(out)],
        capture_output=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert out.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["run"]["id"] == "run-utf8"
