"""Durable execution bookkeeping composed with the existing SQLite repository."""

import sqlite3
from uuid import uuid4

from .clock import utc_now
from .db import Repository
from .errors import DomainError, ErrorCode
from .models import AIModelMetadata, EvaluationRecord, EvaluationRequest
from .work_models import (
    BatchItem,
    BatchRecord,
    BatchRequest,
    ClaimedItem,
    ProviderUsage,
    Reservation,
    UsageRecord,
    UsageSelection,
)


class WorkStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def register_owner(self, owner: str, now: float, lease: float) -> None:
        with self.repository.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO execution_owners (id,expires_at) VALUES (?,?)", (owner, now + lease)
            )
            self._recover(conn, now)

    def heartbeat(self, owner: str, now: float, lease: float) -> None:
        with self.repository.connection(write=True) as conn:
            # Never revive an expired owner: it may already have been fenced out.
            changed = conn.execute(
                "UPDATE execution_owners SET expires_at=? WHERE id=? AND expires_at>?",
                (now + lease, owner, now),
            ).rowcount
            self._recover(conn, now)
        if changed != 1:
            # Recovery must commit even when our own lease was lost.
            raise DomainError("EVALUATION_INTERRUPTED", "Execution owner lease expired")

    @staticmethod
    def _recover(conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            """UPDATE evaluation_executions SET status='interrupted',
               error_code='EVALUATION_INTERRUPTED'
               WHERE status='running' AND owner_id IN
               (SELECT id FROM execution_owners WHERE expires_at<=?)""",
            (now,),
        )
        conn.execute(
            """UPDATE batch_items SET status='interrupted', error_code='EVALUATION_INTERRUPTED'
               WHERE status='running' AND owner_id IN
               (SELECT id FROM execution_owners WHERE expires_at<=?)""",
            (now,),
        )

    def close_owner(self, owner: str, now: float) -> None:
        with self.repository.connection(write=True) as conn:
            conn.execute("UPDATE execution_owners SET expires_at=? WHERE id=?", (now, owner))
            self._recover(conn, now)

    @staticmethod
    def _active(conn: sqlite3.Connection, owner: str, now: float) -> None:
        if not conn.execute(
            "SELECT 1 FROM execution_owners WHERE id=? AND expires_at>?", (owner, now)
        ).fetchone():
            raise DomainError("EVALUATION_INTERRUPTED", "Execution owner lease expired")

    @staticmethod
    def _reservation(row: sqlite3.Row, fingerprint: str) -> Reservation:
        if row["fingerprint"] != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "Key was used with a different request")
        if row["status"] == "succeeded":
            # Replay the initial response, even if the evaluation was subsequently appealed.
            return Reservation(row["id"], EvaluationRecord.model_validate_json(row["response"]))
        if row["status"] == "running":
            raise DomainError("EVALUATION_IN_PROGRESS", "Request is already being processed")
        code: ErrorCode = (
            "EVALUATION_INTERRUPTED" if row["status"] == "interrupted" else "EVALUATION_FAILED"
        )
        raise DomainError(
            code, "Previous execution did not succeed; inspect history before retrying"
        )

    def replay(self, scope: str, key_hash: str, fingerprint: str) -> Reservation | None:
        with self.repository.connection() as conn:
            row = conn.execute(
                "SELECT id,fingerprint,status,response FROM evaluation_executions "
                "WHERE scope=? AND key_hash=?",
                (scope, key_hash),
            ).fetchone()
            return self._reservation(row, fingerprint) if row else None

    def reserve(
        self,
        owner: str,
        now: float,
        scope: str,
        key_hash: str | None,
        fingerprint: str,
        conversation_id: str,
        item_id: str | None = None,
    ) -> Reservation:
        with self.repository.connection(write=True) as conn:
            self._active(conn, owner, now)
            if key_hash:
                row = conn.execute(
                    "SELECT id,fingerprint,status,response FROM evaluation_executions "
                    "WHERE scope=? AND key_hash=?",
                    (scope, key_hash),
                ).fetchone()
                if row:
                    return self._reservation(row, fingerprint)
            if (
                item_id
                and not conn.execute(
                    "SELECT 1 FROM batch_items WHERE id=? AND owner_id=? AND status='running'",
                    (item_id, owner),
                ).fetchone()
            ):
                raise DomainError("EVALUATION_INTERRUPTED", "Batch item is no longer owned")
            execution_id = str(uuid4())
            conn.execute(
                """INSERT INTO evaluation_executions
                (id,owner_id,scope,key_hash,fingerprint,conversation_id,item_id,status,created_at)
                VALUES (?,?,?,?,?,?,?,'running',?)""",
                (
                    execution_id,
                    owner,
                    scope,
                    key_hash,
                    fingerprint,
                    conversation_id,
                    item_id,
                    utc_now(),
                ),
            )
            return Reservation(execution_id)

    def complete(
        self,
        execution_id: str,
        owner: str,
        now: float,
        record: EvaluationRecord,
        duration_ms: int,
        usage: ProviderUsage | None,
    ) -> EvaluationRecord:
        with self.repository.connection(write=True) as conn:
            self._active(conn, owner, now)
            updated = conn.execute(
                """UPDATE evaluation_executions SET status='succeeded', response=?,
                   duration_ms=?, usage=? WHERE id=? AND owner_id=? AND status='running'""",
                (
                    record.model_dump_json(),
                    duration_ms,
                    usage.model_dump_json() if usage else None,
                    execution_id,
                    owner,
                ),
            )
            if updated.rowcount != 1:
                raise DomainError("EVALUATION_INTERRUPTED", "Execution is no longer running")
            self.repository.write_evaluation(conn, record)
            conn.execute(
                "UPDATE evaluation_executions SET evaluation_id=? WHERE id=?",
                (record.id, execution_id),
            )
            conn.execute(
                """UPDATE batch_items SET status='succeeded', evaluation_id=? WHERE id=
                   (SELECT item_id FROM evaluation_executions WHERE id=?)""",
                (record.id, execution_id),
            )
        return record

    def fail(
        self,
        execution_id: str,
        code: str,
        duration_ms: int,
        usage: ProviderUsage | None,
        interrupted: bool = False,
    ) -> None:
        status = "interrupted" if interrupted else "failed"
        with self.repository.connection(write=True) as conn:
            conn.execute(
                """UPDATE evaluation_executions SET status=?,error_code=?,duration_ms=?,usage=?
                   WHERE id=? AND status='running'""",
                (
                    status,
                    code,
                    duration_ms,
                    usage.model_dump_json() if usage else None,
                    execution_id,
                ),
            )
            conn.execute(
                """UPDATE batch_items SET status=?,error_code=? WHERE status='running' AND id=
                   (SELECT item_id FROM evaluation_executions WHERE id=?)""",
                (status, code, execution_id),
            )
            if usage is not None:
                # A lease may have expired while the provider was responding.
                # Keep known usage without reviving the interrupted operation.
                conn.execute(
                    "UPDATE evaluation_executions SET usage=?,duration_ms=? "
                    "WHERE id=? AND status='interrupted' AND usage IS NULL",
                    (usage.model_dump_json(), duration_ms, execution_id),
                )

    def create_batch(self, request: BatchRequest, queue_limit: int) -> BatchRecord:
        batch_id = str(uuid4())
        with self.repository.connection(write=True) as conn:
            queued = conn.execute(
                "SELECT COUNT(*) FROM batch_items WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if queued + len(request.conversation_ids) > queue_limit:
                raise DomainError("BATCH_QUEUE_FULL", "Too many unfinished batch items")
            # Validate all references before inserting any part of the batch.
            if not conn.execute(
                "SELECT 1 FROM checklist_versions WHERE checklist_id=? AND version=?",
                (request.checklist_id, request.checklist_version),
            ).fetchone():
                raise DomainError("NOT_FOUND", "Checklist version not found")
            for conversation_id in request.conversation_ids:
                if not conn.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone():
                    raise DomainError("NOT_FOUND", "Conversation not found")
            conn.execute(
                "INSERT INTO evaluation_batches (id,created_at,checklist_id,checklist_version) "
                "VALUES (?,?,?,?)",
                (batch_id, utc_now(), request.checklist_id, request.checklist_version),
            )
            conn.executemany(
                """INSERT INTO batch_items (id,batch_id,conversation_id,ordinal,status)
                   VALUES (?,?,?,?,'pending')""",
                [
                    (str(uuid4()), batch_id, cid, i)
                    for i, cid in enumerate(request.conversation_ids)
                ],
            )
            return self._batch(conn, batch_id)

    @staticmethod
    def _batch(conn: sqlite3.Connection, batch_id: str) -> BatchRecord:
        row = conn.execute(
            "SELECT id,created_at,checklist_id,checklist_version FROM evaluation_batches WHERE id=?",
            (batch_id,),
        ).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Batch not found")
        items = [
            BatchItem.model_validate(dict(r))
            for r in conn.execute(
                "SELECT id,conversation_id,status,evaluation_id,error_code FROM batch_items "
                "WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            )
        ]
        return BatchRecord(
            **dict(row),
            items=items,
            completed=all(i.status not in {"pending", "running"} for i in items),
        )

    def get_batch(self, batch_id: str) -> BatchRecord:
        with self.repository.connection() as conn:
            return self._batch(conn, batch_id)

    def claim(self, owner: str, now: float) -> ClaimedItem | None:
        with self.repository.connection(write=True) as conn:
            self._active(conn, owner, now)
            row = conn.execute(
                """SELECT i.id,i.batch_id,i.conversation_id,b.checklist_id,b.checklist_version
                FROM batch_items i JOIN evaluation_batches b ON b.id=i.batch_id
                WHERE i.status='pending' ORDER BY b.created_at,b.id,i.ordinal LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE batch_items SET status='running',owner_id=? WHERE id=?", (owner, row["id"])
            )
            return ClaimedItem(
                id=row["id"],
                batch_id=row["batch_id"],
                request=EvaluationRequest(
                    conversation_id=row["conversation_id"],
                    checklist_id=row["checklist_id"],
                    checklist_version=row["checklist_version"],
                ),
            )

    def finish_item(self, item_id: str, owner: str, code: str, *, retry_busy: bool = False) -> None:
        with self.repository.connection(write=True) as conn:
            conn.execute(
                "UPDATE batch_items SET status=?,error_code=?,owner_id=NULL "
                "WHERE id=? AND owner_id=? AND status='running'",
                (
                    "pending" if retry_busy else "failed",
                    None if retry_busy else code,
                    item_id,
                    owner,
                ),
            )

    def get_usage(self, selection: UsageSelection) -> tuple[list[UsageRecord], list[str], int]:
        with self.repository.connection() as conn:
            pending = 0
            if selection.batch_id:
                batch = self._batch(conn, selection.batch_id)
                pending = sum(i.status == "pending" for i in batch.items)
                clause, params = "i.batch_id=?", [selection.batch_id]
            else:
                ids = selection.evaluation_ids or selection.execution_ids
                column = "e.evaluation_id" if selection.evaluation_ids else "e.id"
                clause, params = f"{column} IN ({','.join('?' for _ in ids)})", ids
            rows = conn.execute(
                "SELECT e.id,e.evaluation_id,e.conversation_id,e.status,e.error_code,"
                "e.duration_ms,e.usage,i.batch_id FROM evaluation_executions e "
                "LEFT JOIN batch_items i ON i.id=e.item_id WHERE "
                + clause
                + " AND e.scope='ai' ORDER BY e.created_at,e.id",
                params,
            ).fetchall()
            records = [
                UsageRecord(
                    **{k: row[k] for k in row.keys() if k != "usage"},
                    usage=ProviderUsage.model_validate_json(row["usage"]) if row["usage"] else None,
                )
                for row in rows
            ]
            missing: list[str] = []
            if selection.batch_id:
                found_items = {
                    r[0]
                    for r in conn.execute(
                        "SELECT item_id FROM evaluation_executions WHERE item_id IN "
                        "(SELECT id FROM batch_items WHERE batch_id=?)",
                        (selection.batch_id,),
                    )
                }
                missing = [
                    i.id for i in batch.items if i.id not in found_items and i.status != "pending"
                ]
            elif selection.evaluation_ids:
                found = {r.evaluation_id for r in records}
                for evaluation_id in selection.evaluation_ids:
                    if evaluation_id in found:
                        continue
                    row = conn.execute(
                        "SELECT body FROM evaluations WHERE id=?", (evaluation_id,)
                    ).fetchone()
                    if row:
                        record = EvaluationRecord.model_validate_json(row["body"])
                        if isinstance(record.metadata, AIModelMetadata):
                            m = record.metadata
                            records.append(
                                UsageRecord(
                                    id="legacy:" + record.id,
                                    evaluation_id=record.id,
                                    conversation_id=record.conversation_id,
                                    status="succeeded",
                                    duration_ms=m.duration_ms,
                                    usage=ProviderUsage(
                                        model=m.model,
                                        calls=m.calls,
                                        input_tokens=m.input_tokens,
                                        output_tokens=m.output_tokens,
                                    ),
                                )
                            )
                            continue
                    missing.append(evaluation_id)
            else:
                found_ids = {r.id for r in records}
                missing = [i for i in selection.execution_ids if i not in found_ids]
            return records, missing, pending
