import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.backup import manifest_path, verify_backup
from backend.hosting import HostedSnapshots, create_hosted_app, require_local_storage


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
