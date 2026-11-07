import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.backup import manifest_path, verify_backup
from backend.evaluator import EvaluatorOutput
from backend.hosting import HostedSnapshots, create_hosted_app, require_local_storage
from backend.models import EvaluationResult


def test_hosted_api_static_boundaries_restart_and_backups(monkeypatch, tmp_path, dataset):
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    database, backups = tmp_path / "private" / "live.sqlite3", tmp_path / "backups"
    monkeypatch.setenv("CQA_FRONTEND_PATH", str(frontend))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(database))
    monkeypatch.setenv("CQA_BACKUP_DIRECTORY", str(backups))
    monkeypatch.setenv("CQA_REQUIRE_LOCAL_SQLITE", "0")
    with TestClient(create_hosted_app()) as client:
        assert "Replimetr" in client.get("/").text
        assert "location.origin" in client.get("/api.js").text
        assert client.get("/health").json()["ai_configured"] is False
        assert client.get("/openapi.json").status_code == 200
        dataset_id = client.post("/api/v1/datasets", json=dataset).json()["id"]
        conversation_id = client.get(f"/api/v1/datasets/{dataset_id}/conversations").json()[
            "items"
        ][0]["id"]
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation_id})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_NOT_CONFIGURED"
        for path in ("/.env", "/backend/schema.sql", "/live.sqlite3", "/backups", "/.git/config"):
            assert client.get(path).status_code == 404
    snapshots = sorted(backups.glob("hosted-*.sqlite3"), key=lambda p: p.stat().st_mtime_ns)
    assert len(snapshots) == 2
    assert verify_backup(snapshots[-1])["datasets"] == 1
    with TestClient(create_hosted_app()) as client:
        assert client.get(f"/api/v1/datasets/{dataset_id}/conversations").json()["total"] == 1
    assert len(list(backups.glob("hosted-*.sqlite3"))) == 4
    # Only scheduler-owned pairs are subject to retention.
    preserved = backups / "manual.sqlite3"
    preserved.write_text("not a scheduler file")
    schedule = HostedSnapshots(database, backups)
    for _ in range(7):
        schedule.save()
    assert len(list(backups.glob("hosted-*.sqlite3"))) == 8
    assert preserved.read_text() == "not a scheduler file"
    for path in backups.glob("hosted-*.sqlite3"):
        assert json.loads(manifest_path(path).read_text())["size_bytes"] == path.stat().st_size


@pytest.mark.parametrize("target", ["CQA_DATABASE_PATH", "CQA_BACKUP_DIRECTORY"])
def test_hosting_refuses_public_database_and_backups(monkeypatch, tmp_path, target):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("test")
    monkeypatch.setenv("CQA_FRONTEND_PATH", str(frontend))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(tmp_path / "private.sqlite3"))
    monkeypatch.setenv(target, str(frontend / "private.sqlite3"))
    with pytest.raises(ValueError, match="outside"):
        create_hosted_app()


def test_hosting_refuses_network_mount_before_sqlite(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.hosting.subprocess.run", lambda *a, **k: SimpleNamespace(stdout="nfs\n")
    )
    database = tmp_path / "private" / "db.sqlite3"
    with pytest.raises(RuntimeError, match="local filesystem"):
        require_local_storage(database)
    assert not database.exists()


def test_hosted_adapter_used_by_single_and_batch_and_not_export(
    monkeypatch, tmp_path, dataset, answers
):
    class TestOnlyEvaluator:
        configured = True

        def __init__(self):
            self.inputs = []

        async def evaluate(self, context):
            self.inputs.append(context)
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="synthetic-test-only",
                prompt_version="test",
            )

    monkeypatch.setenv("CQA_FRONTEND_PATH", str(Path(__file__).resolve().parents[2] / "frontend"))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("CQA_BACKUP_DIRECTORY", str(tmp_path / "backups"))
    monkeypatch.setenv("CQA_REQUIRE_LOCAL_SQLITE", "0")
    evaluator = TestOnlyEvaluator()
    with TestClient(create_hosted_app(evaluator=evaluator)) as client:
        assert client.get("/health").json()["ai_configured"] is True
        assert client.get("/").status_code == 200
        dataset_id = client.post("/api/v1/datasets", json=dataset).json()["id"]
        conversation_id = client.get(f"/api/v1/datasets/{dataset_id}/conversations").json()[
            "items"
        ][0]["id"]
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation_id})
        assert response.status_code == 201, response.text
        assert response.json()["metadata"]["model"] == "synthetic-test-only"
        batch = client.post(
            "/api/v1/evaluation-batches",
            json={
                "conversation_ids": [conversation_id],
                "checklist_id": "freedom-broker",
                "checklist_version": 1,
            },
        )
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = client.get(f"/api/v1/evaluation-batches/{batch_id}").json()
            if record["completed"]:
                break
            time.sleep(0.02)
        assert record["items"][0]["status"] == "succeeded"
        assert len(evaluator.inputs) == 2
        assert all(context.conversation.id == conversation_id for context in evaluator.inputs)
        assert client.get(f"/api/v1/datasets/{dataset_id}/export").status_code == 200
        assert len(evaluator.inputs) == 2
    backup = max(
        (tmp_path / "backups").glob("hosted-*.sqlite3"), key=lambda p: p.stat().st_mtime_ns
    )
    assert verify_backup(backup)["evaluations"] == 2
