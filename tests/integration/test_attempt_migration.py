import sqlite3
from pathlib import Path
from time import time

import pytest

from backend.backup import create_backup, verify_backup
from backend.db import Repository
from backend.errors import DomainError
from backend.migrations import migrate_attempts
from backend.work_models import BatchRequest
from backend.work_store import WorkStore


def legacy_copy(source, destination):
    """Recreate the exact old execution constraints using synthetic rows only."""
    schema = Path("backend/schema.sql").read_text(encoding="utf-8")
    old = schema[: schema.index("CREATE TABLE IF NOT EXISTS batch_retry_state")]
    old = old.replace(
        "item_id TEXT REFERENCES batch_items(id)", "item_id TEXT UNIQUE REFERENCES batch_items(id)"
    )
    old = old.replace(", provider_not_called INTEGER NOT NULL DEFAULT 0", "")
    old = old[: old.index("CREATE INDEX IF NOT EXISTS execution_batch_item")]
    with sqlite3.connect(destination) as target, source.connection() as current:
        target.executescript(old)
        tables = [r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [r[1] for r in target.execute(f"PRAGMA table_info({table})")]
            names = ",".join(columns)
            values = ",".join("?" for _ in columns)
            target.executemany(
                f"INSERT INTO {table} ({names}) VALUES ({values})",
                current.execute(f"SELECT {names} FROM {table}").fetchall(),
            )
        target.execute("CREATE INDEX custom_attempt_lookup ON evaluation_executions(fingerprint)")
        target.execute("""CREATE TRIGGER keep_custom_trigger BEFORE UPDATE ON evaluation_executions
                        WHEN NEW.fingerprint='never-used-in-test'
                        BEGIN SELECT RAISE(ABORT, 'synthetic guard'); END""")
    return schema


def seed_attempt(client, conversation):
    store = client.app.state.services.work_store
    store.register_owner("legacy-owner", time(), 600)
    batch = store.create_batch(
        BatchRequest(
            conversation_ids=[conversation["id"]],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
        "legacy-key-hash",
    )
    item = store.claim("legacy-owner", time())
    execution = store.reserve(
        "legacy-owner", time(), "ai", None, "original-fingerprint", conversation["id"], item.id
    )
    store.fail(execution.id, "AI_RATE_LIMITED", 1, None)
    return batch, execution


def test_upgrade_preserves_history_constraints_triggers_and_old_backups(
    client, conversation, evaluation, tmp_path
):
    batch, execution = seed_attempt(client, conversation)
    old = tmp_path / "legacy.sqlite3"
    legacy_copy(client.app.state.repository, old)
    backup = tmp_path / "legacy-backup.sqlite3"
    create_backup(old, backup)
    assert verify_backup(backup)["evaluation_executions"] == 1
    repo = Repository(old)
    repo.initialize()
    with repo.connection() as conn:
        schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]
    repo.initialize()  # Safe on repeated startup.
    with repo.connection() as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == schema_version
    assert repo.get_evaluation(evaluation["id"]).id == evaluation["id"]
    store = WorkStore(repo)
    store.resume_batch(batch.id, 100)
    item = store.claim("legacy-owner", time())
    second = store.reserve(
        "legacy-owner", time(), "ai", None, "new-fingerprint", conversation["id"], item.id
    )
    assert second.id != execution.id
    with repo.connection() as conn:
        rows = conn.execute(
            "SELECT id,status,provider_not_called FROM evaluation_executions ORDER BY created_at"
        ).fetchall()
        assert len(rows) == 2 and rows[0]["status"] == "failed"
        assert all(row["provider_not_called"] == 0 for row in rows)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='evaluation_executions'"
            )
        } >= {
            "execution_owner_status",
            "execution_evaluation",
            "custom_attempt_lookup",
            "keep_custom_trigger",
        }
        assert (
            conn.execute(
                "SELECT batch_id FROM batch_creation_requests WHERE key_hash='legacy-key-hash'"
            ).fetchone()[0]
            == batch.id
        )
    # Existing archives remain readable after the live DB's schema upgrade.
    assert verify_backup(backup)["evaluation_executions"] == 1


def test_failed_migration_rolls_back_old_table_and_rows(client, conversation, tmp_path):
    batch, execution = seed_attempt(client, conversation)
    old = tmp_path / "atomic-legacy.sqlite3"
    schema = legacy_copy(client.app.state.repository, old)
    repo = Repository(old)

    def deny_rename(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

    with pytest.raises(DomainError):
        with repo.connection(write=True) as conn:
            conn.set_authorizer(deny_rename)
            migrate_attempts(conn, schema)
    with repo.connection() as conn:
        assert conn.execute("SELECT id FROM evaluation_executions").fetchone()[0] == execution.id
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='evaluation_executions_upgrade'"
            ).fetchone()
            is None
        )
        assert "provider_not_called" not in [
            r["name"] for r in conn.execute("PRAGMA table_info(evaluation_executions)")
        ]
    repo.initialize()
    assert WorkStore(repo).get_batch(batch.id).items[0].status == "failed"
