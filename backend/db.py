"""SQLite persistence and atomic writes; evaluation rules live in domain.py."""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .clock import utc_now
from .errors import DomainError
from .models import (
    AppealRecord,
    ChecklistDefinition,
    ChecklistSummary,
    ChecklistVersion,
    Conversation,
    ConversationSummary,
    DatasetExport,
    DatasetImport,
    DatasetInfo,
    DatasetSummary,
    EvaluationRecord,
    EvaluationSummary,
    Page,
)

logger = logging.getLogger(__name__)


class Repository:
    def __init__(self, path: Path, timeout: float = 15) -> None:
        self.path = Path(path)
        self.timeout = timeout

    @contextmanager
    def connection(
        self, write: bool = False, *, transaction: bool = True
    ) -> Iterator[sqlite3.Connection]:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if transaction:
                # Explicit read transactions keep evaluations and appeals in one snapshot.
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DomainError(
                    "STORAGE_BUSY", "Storage is busy; retry after reloading the latest state", 503
                ) from None
            # SQLite exception messages may contain SQL or user data.
            logger.error("Storage operation failed (%s)", type(exc).__name__)
            raise DomainError(
                "STORAGE_ERROR", "Storage operation failed; no successful result was recorded", 500
            ) from None
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection(transaction=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        default = ChecklistDefinition.model_validate_json(
            Path(__file__).with_name("default_checklist.json").read_text(encoding="utf-8")
        )
        with self.connection(write=True) as conn:
            stamp = utc_now()
            conn.execute("INSERT OR IGNORE INTO checklists VALUES (?,?)", ("freedom-broker", stamp))
            conn.execute(
                "INSERT OR IGNORE INTO checklist_versions VALUES (?,?,?,?)",
                ("freedom-broker", 1, default.model_dump_json(), stamp),
            )

    def import_dataset(self, payload: DatasetImport) -> DatasetSummary:
        dataset_id, stamp = str(uuid4()), utc_now()
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO datasets VALUES (?,?,?,?)",
                (dataset_id, payload.name, payload.group_id, stamp),
            )
            for index, raw in enumerate(payload.records):
                data = raw.model_dump()
                data.update(id=str(uuid4()), dataset_id=dataset_id)
                data["transcript"] = [
                    {**m.model_dump(), "id": f"m{i + 1}"} for i, m in enumerate(raw.transcript)
                ]
                conversation = Conversation.model_validate(data)
                conn.execute(
                    "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
                    (
                        conversation.id,
                        dataset_id,
                        conversation.conversation_id,
                        conversation.language,
                        conversation.channel,
                        index,
                        conversation.model_dump_json(),
                    ),
                )
        return DatasetSummary(
            id=dataset_id,
            name=payload.name,
            group_id=payload.group_id,
            count=len(payload.records),
            created_at=stamp,
        )

    def list_datasets(self) -> list[DatasetSummary]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT d.*, COUNT(c.id) as count FROM datasets d LEFT JOIN conversations c ON c.dataset_id=d.id GROUP BY d.id ORDER BY d.created_at DESC, d.id"
            ).fetchall()
            return [DatasetSummary.model_validate(dict(r)) for r in rows]

    def list_conversations(
        self,
        dataset_id: str,
        language: str | None = None,
        channel: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Page[ConversationSummary]:
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM datasets WHERE id=?", (dataset_id,)).fetchone():
                raise DomainError("NOT_FOUND", "Dataset not found", 404)
            where, params = ["dataset_id=?"], [dataset_id]
            if language:
                where.append("language=?")
                params.append(language)
            if channel:
                where.append("channel=?")
                params.append(channel)
            clause = " AND ".join(where)
            total = conn.execute(
                f"SELECT COUNT(*) FROM conversations WHERE {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT body FROM conversations WHERE {clause} ORDER BY ordinal LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                record = json.loads(row["body"])
                record["message_count"] = len(record.pop("transcript"))
                items.append(ConversationSummary.model_validate(record))
            return Page(items=items, total=total, limit=limit, offset=offset)

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT body FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Conversation not found", 404)
            return Conversation.model_validate_json(row["body"])

    def create_checklist(self, definition: ChecklistDefinition) -> ChecklistVersion:
        checklist_id, stamp = str(uuid4()), utc_now()
        version = ChecklistVersion(
            **definition.model_dump(), checklist_id=checklist_id, version=1, created_at=stamp
        )
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO checklists VALUES (?,?)", (checklist_id, stamp))
            conn.execute(
                "INSERT INTO checklist_versions VALUES (?,?,?,?)",
                (checklist_id, 1, definition.model_dump_json(), stamp),
            )
        return version

    def list_checklists(self) -> list[ChecklistSummary]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT v.* FROM checklist_versions v JOIN (SELECT checklist_id, MAX(version) version FROM checklist_versions GROUP BY checklist_id) latest USING(checklist_id,version) ORDER BY v.created_at, v.checklist_id"
            ).fetchall()
            return [
                ChecklistSummary(
                    checklist_id=r["checklist_id"],
                    version=r["version"],
                    name=json.loads(r["body"])["name"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def get_checklist(self, checklist_id: str, version: int | None = None) -> ChecklistVersion:
        with self.connection() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT * FROM checklist_versions WHERE checklist_id=? ORDER BY version DESC LIMIT 1",
                    (checklist_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM checklist_versions WHERE checklist_id=? AND version=?",
                    (checklist_id, version),
                ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Checklist version not found", 404)
            return ChecklistVersion(
                **json.loads(row["body"]),
                checklist_id=checklist_id,
                version=row["version"],
                created_at=row["created_at"],
            )

    def new_version(
        self, checklist_id: str, expected: int, definition: ChecklistDefinition
    ) -> ChecklistVersion:
        with self.connection(write=True) as conn:
            latest = conn.execute(
                "SELECT MAX(version) FROM checklist_versions WHERE checklist_id=?", (checklist_id,)
            ).fetchone()[0]
            if latest is None:
                raise DomainError("NOT_FOUND", "Checklist not found", 404)
            if latest != expected:
                raise DomainError(
                    "VERSION_CONFLICT", "Checklist changed; reload its latest version", 409
                )
            version = ChecklistVersion(
                **definition.model_dump(),
                checklist_id=checklist_id,
                version=latest + 1,
                created_at=utc_now(),
            )
            conn.execute(
                "INSERT INTO checklist_versions VALUES (?,?,?,?)",
                (checklist_id, version.version, definition.model_dump_json(), version.created_at),
            )
        return version

    def insert_evaluation(self, record: EvaluationRecord) -> EvaluationRecord:
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.conversation_id,
                    record.checklist.checklist_id,
                    record.checklist.version,
                    record.origin,
                    record.revision,
                    record.created_at,
                    record.model_dump_json(exclude={"appeals"}),
                ),
            )
        return record

    @staticmethod
    def _get_evaluation(conn: sqlite3.Connection, evaluation_id: str) -> EvaluationRecord:
        row = conn.execute("SELECT body FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
        if not row:
            raise DomainError("NOT_FOUND", "Evaluation not found", 404)
        record = EvaluationRecord.model_validate_json(row["body"])
        record.appeals = [
            AppealRecord.model_validate_json(r["body"])
            for r in conn.execute(
                "SELECT body FROM appeals WHERE evaluation_id=? ORDER BY created_at, id",
                (evaluation_id,),
            )
        ]
        return record

    def get_evaluation(self, evaluation_id: str) -> EvaluationRecord:
        with self.connection() as conn:
            return self._get_evaluation(conn, evaluation_id)

    def list_evaluations(
        self, conversation_id: str, limit: int = 100, offset: int = 0
    ) -> Page[EvaluationSummary]:
        with self.connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone():
                raise DomainError("NOT_FOUND", "Conversation not found", 404)
            total = conn.execute(
                "SELECT COUNT(*) FROM evaluations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id,origin,revision,created_at,body FROM evaluations WHERE conversation_id=? ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                (conversation_id, limit, offset),
            ).fetchall()
            items = [
                EvaluationSummary(
                    id=r["id"],
                    origin=r["origin"],
                    revision=r["revision"],
                    created_at=r["created_at"],
                    score=json.loads(r["body"])["score"]["total"],
                )
                for r in rows
            ]
            return Page(items=items, total=total, limit=limit, offset=offset)

    def get_appeal(self, appeal_id: str) -> AppealRecord:
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Appeal not found", 404)
            return AppealRecord.model_validate_json(row["body"])

    def insert_appeal(self, record: AppealRecord) -> AppealRecord:
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT revision FROM evaluations WHERE id=?", (record.evaluation_id,)
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Evaluation not found", 404)
            if row["revision"] != record.opened_revision:
                raise DomainError(
                    "REVISION_CONFLICT", "Evaluation changed; reload before opening an appeal", 409
                )
            if conn.execute(
                "SELECT 1 FROM appeals WHERE evaluation_id=? AND criterion_id=? AND status='open'",
                (record.evaluation_id, record.criterion_id),
            ).fetchone():
                raise DomainError(
                    "APPEAL_EXISTS", "An open appeal already exists for this criterion", 409
                )
            conn.execute(
                "INSERT INTO appeals VALUES (?,?,?,?,?,?)",
                (
                    record.id,
                    record.evaluation_id,
                    record.criterion_id,
                    record.status,
                    record.created_at,
                    record.model_dump_json(),
                ),
            )
        return record

    def commit_review(
        self, evaluation: EvaluationRecord, appeal: AppealRecord, expected_revision: int
    ) -> EvaluationRecord:
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT status FROM appeals WHERE id=? AND evaluation_id=?",
                (appeal.id, evaluation.id),
            ).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Appeal not found", 404)
            if row["status"] != "open":
                raise DomainError("APPEAL_CLOSED", "This appeal was already resolved", 409)
            # Compare-and-swap prevents two reviewers from overwriting each other.
            cursor = conn.execute(
                "UPDATE evaluations SET body=?, revision=? WHERE id=? AND revision=?",
                (
                    evaluation.model_dump_json(exclude={"appeals"}),
                    evaluation.revision,
                    evaluation.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise DomainError(
                    "REVISION_CONFLICT",
                    "Evaluation changed; reload before submitting a decision",
                    409,
                )
            conn.execute(
                "UPDATE appeals SET body=?,status='resolved' WHERE id=?",
                (appeal.model_dump_json(), appeal.id),
            )
            # Read before committing: the response describes this exact decision.
            result = self._get_evaluation(conn, evaluation.id)
        return result

    def export_dataset(self, dataset_id: str) -> DatasetExport:
        with self.connection() as conn:
            dataset = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if not dataset:
                raise DomainError("NOT_FOUND", "Dataset not found", 404)
            conversations = [
                Conversation.model_validate_json(r["body"])
                for r in conn.execute(
                    "SELECT body FROM conversations WHERE dataset_id=? ORDER BY ordinal",
                    (dataset_id,),
                )
            ]
            evaluations = [
                EvaluationRecord.model_validate_json(r["body"])
                for r in conn.execute(
                    "SELECT e.body FROM evaluations e JOIN conversations c ON c.id=e.conversation_id WHERE c.dataset_id=? ORDER BY e.created_at, e.id",
                    (dataset_id,),
                )
            ]
            by_id = {evaluation.id: evaluation for evaluation in evaluations}
            for row in conn.execute(
                "SELECT a.body FROM appeals a JOIN evaluations e ON e.id=a.evaluation_id JOIN conversations c ON c.id=e.conversation_id WHERE c.dataset_id=? ORDER BY a.created_at, a.id",
                (dataset_id,),
            ):
                appeal = AppealRecord.model_validate_json(row["body"])
                by_id[appeal.evaluation_id].appeals.append(appeal)
            return DatasetExport(
                exported_at=utc_now(),
                dataset=DatasetInfo.model_validate(dict(dataset)),
                conversations=conversations,
                evaluations=evaluations,
            )
