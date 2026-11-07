import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from time import time

import pytest
from fastapi.testclient import TestClient

from backend import backup
from backend.db import Repository
from backend.domain import new_evaluation
from backend.idempotency import request_fingerprint
from backend.main import create_app
from backend.models import AIModelMetadata, EvaluationRequest
from backend.work_models import BatchRequest
from backend.work_store import WorkStore


@pytest.fixture
def snapshot(client, settings, tmp_path):
    output = tmp_path / "backup.sqlite3"
    metadata = backup.create_backup(settings.database_path, output)
    return output, metadata


def rewrite_manifest(path):
    manifest = backup.BackupManifest.model_validate_json(backup.manifest_path(path).read_bytes())
    manifest.size_bytes = path.stat().st_size
    manifest.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    backup.manifest_path(path).write_text(manifest.model_dump_json(), encoding="utf-8")


def damage_synthetic_index(path):
    # Deliberately corrupt only this temporary test artifact, after its hash was
    # created, to exercise SQLite integrity checks independently from SHA-256.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE synthetic_check(value INTEGER)")
        conn.execute("CREATE INDEX synthetic_index ON synthetic_check(value)")
        conn.execute("INSERT INTO synthetic_check VALUES (1)")
        definition = conn.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master WHERE name='synthetic_index'"
        ).fetchone()
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("DELETE FROM sqlite_master WHERE name='synthetic_index'")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO synthetic_check VALUES (2)")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES (?,?,?,?,?)",
            definition,
        )


def test_backup_uses_consistent_snapshot_while_wal_writer_commits(
    client, settings, tmp_path, monkeypatch
):
    repo = client.app.state.repository
    with repo.connection(write=True) as conn:
        conn.execute("CREATE TABLE synthetic_counters(id INTEGER PRIMARY KEY, value INTEGER)")
        conn.executemany("INSERT INTO synthetic_counters VALUES (?,0)", [(1,), (2,)])
        conn.execute("CREATE TABLE synthetic_payload(body BLOB)")
        conn.execute("INSERT INTO synthetic_payload VALUES (zeroblob(3000000))")
    ready, commit, committed, close = (threading.Event() for _ in range(4))

    def writer():
        with sqlite3.connect(settings.database_path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE synthetic_counters SET value=1 WHERE id=1")
            ready.set()
            assert commit.wait(5)
            conn.execute("UPDATE synthetic_counters SET value=1 WHERE id=2")
            conn.commit()
            committed.set()
            assert close.wait(5)

    real_monotonic = backup.monotonic
    progress_calls = 0

    def commit_during_backup_progress():
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls == 2:  # Deadline setup, then first incremental backup step.
            commit.set()
            assert committed.wait(3)
        return real_monotonic()

    monkeypatch.setattr(backup, "monotonic", commit_during_backup_progress)
    output = tmp_path / "online.sqlite3"
    with ThreadPoolExecutor(1) as pool:
        task = pool.submit(writer)
        try:
            assert ready.wait(3)
            assert Path(str(settings.database_path) + "-wal").exists()
            metadata = backup.create_backup(settings.database_path, output)
        finally:
            commit.set()
            close.set()
        task.result(5)
    assert committed.is_set() and progress_calls > 2
    with sqlite3.connect(output) as conn:
        values = [r[0] for r in conn.execute("SELECT value FROM synthetic_counters")]
        assert values[0] == values[1] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert metadata.size_bytes == output.stat().st_size
    assert metadata.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert metadata.created_at.tzinfo is not None
    assert backup.verify_backup(output)["checklist_versions"] == 1


def test_backup_restores_history_and_never_replays_unfinished_calls(
    client, settings, conversation, answers, tmp_path, monkeypatch
):
    payload = {"conversation_id": conversation["id"], "author": "QA", "result": answers}
    initial = client.post(
        "/api/v1/evaluations/manual", json=payload, headers={"Idempotency-Key": "manual-history"}
    )
    assert initial.status_code == 201
    evaluation_id = initial.json()["id"]
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation_id}/appeals",
        json={
            "criterion_id": "contact",
            "author": "QA",
            "comment": "Synthetic review",
        },
    ).json()
    result = client.post(
        f"/api/v1/appeals/{appeal['id']}/decision",
        json={
            "expected_revision": 1,
            "value": "no",
            "reason": "Synthetic correction",
            "reviewer": "QA",
            "evidence_kind": "missing",
            "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
        },
    )
    assert result.status_code == 200
    repo, store = client.app.state.repository, client.app.state.services.work_store
    reference = repo.get_evaluation(evaluation_id)
    owner = "synthetic-owner"
    store.register_owner(owner, time(), 60)
    batch_request = BatchRequest(
        conversation_ids=[conversation["id"]], checklist_id="freedom-broker", checklist_version=1
    )
    finished = store.create_batch(batch_request, 100)
    item = store.claim(owner, time())
    execution = store.reserve(owner, time(), "ai", None, "synthetic", conversation["id"], item.id)
    ai = new_evaluation(
        reference.conversation,
        reference.checklist,
        deepcopy(reference.original),
        AIModelMetadata(model="synthetic-only", prompt_version="v1", duration_ms=1),
        record_id="synthetic-ai",
        created_at=reference.created_at,
    )
    store.complete(execution.id, owner, time(), ai, 1, None)
    running = store.create_batch(batch_request, 100)
    item = store.claim(owner, time())
    request = EvaluationRequest(conversation_id=conversation["id"])
    key_hash = hashlib.sha256(b"inflight-history").hexdigest()
    inflight = store.reserve(
        owner, time(), "ai", key_hash, request_fingerprint(request), conversation["id"], item.id
    )
    pending = store.create_batch(batch_request, 100, hashlib.sha256(b"pending-history").hexdigest())
    source_statuses = [store.get_batch(b.id) for b in [finished, running, pending]]
    output, restored = tmp_path / "history.sqlite3", tmp_path / "restored.sqlite3"
    backup.create_backup(settings.database_path, output)
    original_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    counts = backup.verify_backup(output)
    restored_info = backup.restore_backup(output, restored)
    assert restored_info["records"] == counts
    assert restored_info["suspended"] == {"executions": 1, "batch_items": 2}
    assert hashlib.sha256(output.read_bytes()).hexdigest() == original_hash
    assert [store.get_batch(b.id) for b in [finished, running, pending]] == source_statuses
    restored_repo = Repository(restored)
    assert restored_repo.get_evaluation(evaluation_id) == reference
    assert restored_repo.get_evaluation(ai.id) == ai
    assert restored_repo.get_appeal(appeal["id"]) == repo.get_appeal(appeal["id"])
    checked = threading.Event()
    claim = WorkStore.claim

    def check_no_work(self, owner, now):
        result = claim(self, owner, now)
        assert result is None
        checked.set()
        return result

    class NoCalls:
        configured = True
        calls = 0

        async def evaluate(self, context):
            self.calls += 1
            raise AssertionError("Restored work must require human review")

    model = NoCalls()
    monkeypatch.setattr(WorkStore, "claim", check_no_work)
    with TestClient(create_app(replace(settings, database_path=restored), model)) as recovered:
        assert checked.wait(3)
        for batch in [running, pending]:
            item = recovered.get(f"/api/v1/evaluation-batches/{batch.id}").json()["items"][0]
            assert (
                item["status"] == "interrupted" and item["error_code"] == "RESTORE_REQUIRES_REVIEW"
            )
            usage = recovered.post("/api/v1/reports/usage", json={"batch_id": batch.id}).json()
            assert not usage["calls_complete"]  # Source may have run pending work since snapshot.
        assert (
            recovered.get(f"/api/v1/evaluation-batches/{finished.id}").json()["items"][0]["status"]
            == "succeeded"
        )
        replay = recovered.post(
            "/api/v1/evaluations",
            json=request.model_dump(),
            headers={"Idempotency-Key": "inflight-history"},
        )
        assert (
            replay.status_code == 409 and replay.json()["error"]["code"] == "EVALUATION_INTERRUPTED"
        )
        manual = recovered.post(
            "/api/v1/evaluations/manual",
            json=payload,
            headers={"Idempotency-Key": "manual-history"},
        )
        assert manual.json() == initial.json()
        idem_batch = recovered.post(
            "/api/v1/evaluation-batches",
            json=batch_request.model_dump(),
            headers={"Idempotency-Key": "pending-history"},
        )
        assert idem_batch.json() == pending.model_dump()  # Initial response remains immutable.
        usage = recovered.post(
            "/api/v1/reports/usage", json={"execution_ids": [inflight.id]}
        ).json()
        assert not usage["complete"]
    assert model.calls == 0


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_existing_destination_and_live_database_are_never_overwritten(
    snapshot, settings, tmp_path, operation
):
    source, _ = snapshot
    destination = tmp_path / "existing.sqlite3"
    destination.write_bytes(b"keep this file")
    for target in [destination, settings.database_path, source]:
        before = target.read_bytes()
        with pytest.raises(backup.BackupError, match="already exists") as error:
            if operation == "create":
                backup.create_backup(settings.database_path, target)
            else:
                backup.restore_backup(source, target)
        assert error.value.code == "DESTINATION_EXISTS" and target.read_bytes() == before


@pytest.mark.parametrize(
    "corruption,code",
    [
        ("wrong_hash", "HASH_MISMATCH"),
        ("truncated", "HASH_MISMATCH"),
        ("invalid_sqlite", "DATABASE_ERROR"),
        ("invalid_fk", "FOREIGN_KEYS_FAILED"),
        ("integrity", "INTEGRITY_FAILED"),
        ("invalid_record", "INVALID_CONTENT"),
        ("invalid_manifest", "INVALID_CONTENT"),
    ],
)
def test_verify_and_restore_reject_bad_backups(snapshot, tmp_path, corruption, code):
    source, _ = snapshot
    if corruption == "wrong_hash":
        data = json.loads(backup.manifest_path(source).read_text())
        data["sha256"] = "0" * 64
        backup.manifest_path(source).write_text(json.dumps(data))
    elif corruption == "invalid_manifest":
        backup.manifest_path(source).write_text("{}")
    elif corruption in {"truncated", "invalid_sqlite"}:
        source.write_bytes(b"not a database")
        if corruption == "invalid_sqlite":
            rewrite_manifest(source)
    elif corruption == "integrity":
        damage_synthetic_index(source)
        rewrite_manifest(source)
    else:
        with sqlite3.connect(source) as conn:
            if corruption == "invalid_fk":
                conn.execute("INSERT INTO checklist_versions VALUES ('missing',1,'{}','2026')")
            else:
                conn.execute("UPDATE checklist_versions SET body='{}'")
        rewrite_manifest(source)
    destination = tmp_path / "must-not-exist.sqlite3"
    for action in [
        lambda: backup.verify_backup(source),
        lambda: backup.restore_backup(source, destination),
    ]:
        with pytest.raises(backup.BackupError) as error:
            action()
        assert error.value.code == code
        assert not destination.exists()


def test_backup_rejects_orphan_sidecars_and_manifest_and_handles_missing_paths(
    snapshot, settings, tmp_path
):
    source, _ = snapshot
    dest = tmp_path / "new.sqlite3"
    sidecar = Path(str(dest) + "-wal")
    sidecar.write_bytes(b"existing WAL")
    with pytest.raises(backup.BackupError) as error:
        backup.restore_backup(source, dest)
    assert error.value.code == "DESTINATION_EXISTS" and sidecar.read_bytes() == b"existing WAL"
    sidecar.unlink()
    backup.manifest_path(dest).write_bytes(b"existing manifest")
    with pytest.raises(backup.BackupError) as error:
        backup.create_backup(settings.database_path, dest)
    assert error.value.code == "DESTINATION_EXISTS" and not dest.exists()
    for action in [
        lambda: backup.create_backup(tmp_path / "missing.sqlite3", dest),
        lambda: backup.restore_backup(source, tmp_path / "missing-dir" / "new.sqlite3"),
    ]:
        with pytest.raises(backup.BackupError) as error:
            action()
        assert error.value.code == "FILESYSTEM_ERROR"


def test_snapshot_timeout_does_not_publish_partial_files(client, settings, tmp_path, monkeypatch):
    ticks = iter([0, 2])
    monkeypatch.setattr(backup, "monotonic", lambda: next(ticks))
    output = tmp_path / "timeout.sqlite3"
    with pytest.raises(backup.BackupError) as error:
        backup.create_backup(settings.database_path, output, timeout=1)
    assert error.value.code == "BACKUP_TIMEOUT"
    assert not output.exists() and not backup.manifest_path(output).exists()
    assert not list(tmp_path.glob(".cqa-backup-*"))


def test_publish_race_preserves_other_file_and_rolls_back_own_link(
    client, settings, tmp_path, monkeypatch
):
    output = tmp_path / "race.sqlite3"
    real_link = os.link

    def concurrent_manifest_writer(source, destination):
        if destination == backup.manifest_path(output):
            destination.write_bytes(b"written by another process")
        real_link(source, destination)

    monkeypatch.setattr(backup.os, "link", concurrent_manifest_writer)
    with pytest.raises(backup.BackupError) as error:
        backup.create_backup(settings.database_path, output)
    assert error.value.code == "DESTINATION_EXISTS"
    assert not output.exists()
    assert backup.manifest_path(output).read_bytes() == b"written by another process"
    assert not list(tmp_path.glob(".cqa-backup-*"))


def test_filesystem_publish_error_is_explicit_and_leaves_no_partial_backup(
    client, settings, tmp_path, monkeypatch
):
    def denied(*args, **kwargs):
        raise PermissionError("synthetic")

    monkeypatch.setattr(backup.os, "link", denied)
    output = tmp_path / "denied.sqlite3"
    with pytest.raises(backup.BackupError) as error:
        backup.create_backup(settings.database_path, output)
    assert error.value.code == "FILESYSTEM_ERROR"
    assert not output.exists() and not backup.manifest_path(output).exists()


def test_cli_commands_use_explicit_files_and_verification_never_starts_worker(
    client, settings, tmp_path
):
    source, restored = tmp_path / "cli.sqlite3", tmp_path / "restored.sqlite3"
    operations = [
        ["create", "--database", str(settings.database_path), "--output", str(source)],
        ["verify", "--backup", str(source)],
        ["restore", "--backup", str(source), "--output", str(restored)],
    ]
    with client.app.state.repository.connection() as conn:
        owners = conn.execute("SELECT COUNT(*) FROM execution_owners").fetchone()[0]
    for arguments in operations:
        result = subprocess.run(
            [sys.executable, "-m", "backend.backup", *arguments],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert isinstance(json.loads(result.stdout), dict)
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT COUNT(*) FROM execution_owners").fetchone()[0] == owners
        assert conn.execute("SELECT COUNT(*) FROM evaluation_executions").fetchone()[0] == 0
    failed = subprocess.run(
        [sys.executable, "-m", "backend.backup", *operations[2]],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert failed.returncode == 1 and "DESTINATION_EXISTS" in failed.stderr
