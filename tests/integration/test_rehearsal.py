"""Real loopback processes and CLI rehearsal; no mock model and no external services."""

import multiprocessing
import os
import sys

import httpx
import pytest

from scripts import rehearse_backend as rehearsal


def test_rehearsal_isolates_environment_and_removes_owned_files(monkeypatch, tmp_path, capsys):
    working_db = tmp_path / "working.sqlite3"
    working_db.write_bytes(b"Do not open or initialize this working database")
    dotenv = tmp_path / ".env"
    dotenv.write_text("CQA_DATABASE_PATH=do-not-create.sqlite3\n", encoding="utf-8")
    runs = tmp_path / "temporary-runs"
    runs.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rehearsal.tempfile, "tempdir", str(runs))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(working_db))
    monkeypatch.setenv("CQA_TARIFFS_PATH", str(tmp_path / "must-not-be-read.json"))
    monkeypatch.setenv("CQA_ORGANIZER_API_URL", "https://never-contact.invalid")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("CQA_SUBMISSION_HTTP_USER", "unpaired-user-must-not-leak")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    before_env = dict(os.environ)
    before_db, before_dotenv = working_db.read_bytes(), dotenv.read_bytes()
    before_children = {process.pid for process in multiprocessing.active_children()}

    rehearsal.rehearse()

    assert dict(os.environ) == before_env
    assert working_db.read_bytes() == before_db and dotenv.read_bytes() == before_dotenv
    assert not (tmp_path / "do-not-create.sqlite3").exists()
    assert not (tmp_path / "results").exists()
    assert list(runs.iterdir()) == []
    assert {process.pid for process in multiprocessing.active_children()} == before_children
    output = capsys.readouterr().out
    assert "16 existing API steps" in output
    assert "DEV 65 explicit errors" in output and "PROD 10 explicit errors" in output
    assert "expected AI_NOT_CONFIGURED" in output
    assert "process restart preserved" in output and "temporary files removed" in output


def test_failure_stops_owned_process_and_keeps_other_server_alive(tmp_path):
    other, owned = tmp_path / "other", tmp_path / "owned"
    other.mkdir()
    owned.mkdir()
    with rehearsal.LocalServer(other) as unrelated:
        with pytest.raises(rehearsal.DemoFailure, match="Deliberate rehearsal failure"):
            with rehearsal.LocalServer(owned) as server:
                raise rehearsal.DemoFailure("Deliberate rehearsal failure")
        assert not server.process.is_alive()
        with httpx.Client(trust_env=False) as client:
            assert client.get(unrelated.url + "/health").json()["ai_configured"] is False
    assert not unrelated.process.is_alive()


def test_rehearsal_refuses_temp_files_inside_repository(monkeypatch):
    monkeypatch.setattr(rehearsal.tempfile, "gettempdir", lambda: str(rehearsal.ROOT / "results"))
    with pytest.raises(rehearsal.DemoFailure, match="outside the repository"):
        rehearsal.rehearse()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (
            rehearsal.DemoFailure("Expected AI_NOT_CONFIGURED but received a successful result"),
            "Expected AI_NOT_CONFIGURED",
        ),
        (httpx.ReadTimeout("Local HTTP response timed out"), "ReadTimeout: Local HTTP"),
    ],
)
def test_cli_failure_returns_nonzero_with_reason(monkeypatch, capsys, error, reason):
    def fail():
        raise error

    monkeypatch.setattr(rehearsal, "rehearse", fail)
    monkeypatch.setattr(sys, "argv", ["rehearse_backend"])
    with pytest.raises(SystemExit) as exc:
        rehearsal.main()
    assert exc.value.code == 1
    assert reason in capsys.readouterr().err
