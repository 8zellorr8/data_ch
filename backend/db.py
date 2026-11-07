from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import sqlite3

from .errors import DomainError
from .models import ChecklistDefinition, ChecklistVersion, DatasetImport, Conversation, EvaluationResult, AppealDecision
from .scoring import calculate, check_value, validate_result


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, name TEXT NOT NULL, group_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id), external_id TEXT NOT NULL, language TEXT NOT NULL, channel TEXT NOT NULL, ordinal INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(dataset_id, external_id));
CREATE INDEX IF NOT EXISTS conversation_dataset ON conversations(dataset_id, ordinal);
CREATE TABLE IF NOT EXISTS checklists(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checklist_versions(checklist_id TEXT NOT NULL REFERENCES checklists(id), version INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(checklist_id, version));
CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id), checklist_id TEXT NOT NULL, checklist_version INTEGER NOT NULL, origin TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL, FOREIGN KEY(checklist_id, checklist_version) REFERENCES checklist_versions(checklist_id, version));
CREATE INDEX IF NOT EXISTS evaluation_conversation ON evaluations(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS appeals(id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id), criterion_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_appeal ON appeals(evaluation_id, criterion_id) WHERE status='open';
"""


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        default = ChecklistDefinition.model_validate_json(Path(__file__).with_name("default_checklist.json").read_text(encoding="utf-8"))
        with self.connection(write=True) as conn:
            stamp = now()
            conn.execute("INSERT OR IGNORE INTO checklists VALUES (?,?)", ("freedom-broker", stamp))
            conn.execute("INSERT OR IGNORE INTO checklist_versions VALUES (?,?,?,?)", ("freedom-broker", 1, dump(default.model_dump()), stamp))

    def import_dataset(self, payload: DatasetImport):
        dataset_id, stamp = str(uuid4()), now()
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO datasets VALUES (?,?,?,?)", (dataset_id, payload.name, payload.group_id, stamp))
            for index, raw in enumerate(payload.records):
                data = raw.model_dump()
                data.update(id=str(uuid4()), dataset_id=dataset_id)
                data["transcript"] = [{**m.model_dump(), "id": f"m{i+1}"} for i, m in enumerate(raw.transcript)]
                conversation = Conversation.model_validate(data)
                conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", (conversation.id, dataset_id, conversation.conversation_id, conversation.language, conversation.channel, index, dump(conversation.model_dump())))
        return {"id": dataset_id, "name": payload.name, "group_id": payload.group_id, "count": len(payload.records), "created_at": stamp}

    def list_datasets(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT d.*, COUNT(c.id) as count FROM datasets d LEFT JOIN conversations c ON c.dataset_id=d.id GROUP BY d.id ORDER BY d.created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def list_conversations(self, dataset_id, language=None, channel=None, limit=100, offset=0):
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM datasets WHERE id=?", (dataset_id,)).fetchone():
                raise DomainError("NOT_FOUND", "Dataset not found", 404)
            where, params = ["dataset_id=?"], [dataset_id]
            if language:
                where.append("language=?"); params.append(language)
            if channel:
                where.append("channel=?"); params.append(channel)
            clause = " AND ".join(where)
            total = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(f"SELECT body FROM conversations WHERE {clause} ORDER BY ordinal LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            items = []
            for row in rows:
                record = json.loads(row["body"])
                record["message_count"] = len(record.pop("transcript"))
                items.append(record)
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_conversation(self, conversation_id):
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Conversation not found", 404)
            return Conversation.model_validate_json(row["body"])

    def create_checklist(self, definition: ChecklistDefinition):
        checklist_id, stamp = str(uuid4()), now()
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO checklists VALUES (?,?)", (checklist_id, stamp))
            conn.execute("INSERT INTO checklist_versions VALUES (?,?,?,?)", (checklist_id, 1, dump(definition.model_dump()), stamp))
        return self.get_checklist(checklist_id, 1)

    def list_checklists(self):
        with self.connection() as conn:
            rows = conn.execute("SELECT v.* FROM checklist_versions v JOIN (SELECT checklist_id, MAX(version) version FROM checklist_versions GROUP BY checklist_id) latest USING(checklist_id,version) ORDER BY v.created_at").fetchall()
            return [{"checklist_id": r["checklist_id"], "version": r["version"], "name": json.loads(r["body"])["name"], "created_at": r["created_at"]} for r in rows]

    def get_checklist(self, checklist_id, version=None):
        with self.connection() as conn:
            if version is None:
                row = conn.execute("SELECT * FROM checklist_versions WHERE checklist_id=? ORDER BY version DESC LIMIT 1", (checklist_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM checklist_versions WHERE checklist_id=? AND version=?", (checklist_id, version)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Checklist version not found", 404)
            return ChecklistVersion(**json.loads(row["body"]), checklist_id=checklist_id, version=row["version"], created_at=row["created_at"])

    def new_version(self, checklist_id, expected, definition):
        with self.connection(write=True) as conn:
            latest = conn.execute("SELECT MAX(version) FROM checklist_versions WHERE checklist_id=?", (checklist_id,)).fetchone()[0]
            if latest is None:
                raise DomainError("NOT_FOUND", "Checklist not found", 404)
            if latest != expected:
                raise DomainError("VERSION_CONFLICT", "Checklist changed; reload its latest version", 409)
            version = latest + 1
            conn.execute("INSERT INTO checklist_versions VALUES (?,?,?,?)", (checklist_id, version, dump(definition.model_dump()), now()))
        return self.get_checklist(checklist_id, version)

    def save_evaluation(self, conversation, checklist, result, origin, metadata):
        validate_result(checklist, conversation, result)
        snapshot = {"conversation": conversation.model_dump(), "checklist": checklist.model_dump()}
        body = {
            "id": str(uuid4()), "conversation_id": conversation.id, "origin": origin, "revision": 1,
            "created_at": now(), **snapshot,
            "input_hash": hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            "original": result.model_dump(), "final": result.model_dump(), "original_score": calculate(checklist, result),
            "score": calculate(checklist, result), "metadata": metadata, "reviews": [],
        }
        with self.connection(write=True) as conn:
            conn.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?,?,?)", (body["id"], conversation.id, checklist.checklist_id, checklist.version, origin, 1, body["created_at"], dump(body)))
        return {**body, "appeals": []}

    def get_evaluation(self, evaluation_id):
        with self.connection() as conn:
            row = conn.execute("SELECT body FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Evaluation not found", 404)
            appeals = conn.execute("SELECT body FROM appeals WHERE evaluation_id=? ORDER BY created_at", (evaluation_id,)).fetchall()
            return {**json.loads(row["body"]), "appeals": [json.loads(r["body"]) for r in appeals]}

    def list_evaluations(self, conversation_id, limit=100, offset=0):
        self.get_conversation(conversation_id)
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM evaluations WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            rows = conn.execute("SELECT id,origin,revision,created_at,body FROM evaluations WHERE conversation_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?", (conversation_id, limit, offset)).fetchall()
            return {"items": [{"id": r["id"], "origin": r["origin"], "revision": r["revision"], "created_at": r["created_at"], "score": json.loads(r["body"])["score"]["total"]} for r in rows], "total": total, "limit": limit, "offset": offset}

    def create_appeal(self, evaluation_id, payload):
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT body FROM evaluations WHERE id=?", (evaluation_id,)).fetchone()
            if not row:
                raise DomainError("NOT_FOUND", "Evaluation not found", 404)
            evaluation = json.loads(row["body"])
            if payload.criterion_id not in {c["id"] for c in evaluation["checklist"]["criteria"]}:
                raise DomainError("UNKNOWN_CRITERION", "Criterion is not in this evaluation", 422)
            body = {"id": str(uuid4()), "evaluation_id": evaluation_id, **payload.model_dump(), "status": "open", "opened_revision": evaluation["revision"], "created_at": now(), "decision": None}
            try:
                conn.execute("INSERT INTO appeals VALUES (?,?,?,?,?,?)", (body["id"], evaluation_id, payload.criterion_id, "open", body["created_at"], dump(body)))
            except sqlite3.IntegrityError:
                raise DomainError("APPEAL_EXISTS", "An open appeal already exists for this criterion", 409) from None
        return body

    def decide_appeal(self, appeal_id, decision: AppealDecision):
        with self.connection(write=True) as conn:
            appeal_row = conn.execute("SELECT body FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            if not appeal_row:
                raise DomainError("NOT_FOUND", "Appeal not found", 404)
            appeal = json.loads(appeal_row["body"])
            if appeal["status"] != "open":
                raise DomainError("APPEAL_CLOSED", "This appeal was already resolved", 409)
            row = conn.execute("SELECT body FROM evaluations WHERE id=?", (appeal["evaluation_id"],)).fetchone()
            evaluation = json.loads(row["body"])
            if evaluation["revision"] != decision.expected_revision:
                raise DomainError("REVISION_CONFLICT", "Evaluation changed; reload before submitting a decision", 409)
            checklist = ChecklistVersion.model_validate(evaluation["checklist"])
            criterion = next(c for c in checklist.criteria if c.id == appeal["criterion_id"])
            check_value(criterion, decision.value)
            answer = next(a for a in evaluation["final"]["answers"] if a["criterion_id"] == criterion.id)
            before = answer["value"]
            answer.update(value=decision.value, explanation=decision.reason, na_reason=decision.reason if decision.value == "na" else "", needs_review=False, limitation="")
            answer["evidence_kind"] = decision.evidence_kind
            answer["evidence"] = [e.model_dump() for e in decision.evidence]
            result = EvaluationResult.model_validate(evaluation["final"])
            validate_result(checklist, Conversation.model_validate(evaluation["conversation"]), result)
            evaluation["score"] = calculate(checklist, result)
            evaluation["revision"] += 1
            review = {"appeal_id": appeal_id, "criterion_id": criterion.id, "previous_value": before, **decision.model_dump(exclude={"expected_revision"}), "created_at": now()}
            evaluation["reviews"].append(review)
            appeal.update(status="resolved", decision=review)
            conn.execute("UPDATE evaluations SET body=?,revision=? WHERE id=?", (dump(evaluation), evaluation["revision"], evaluation["id"]))
            conn.execute("UPDATE appeals SET body=?,status='resolved' WHERE id=?", (dump(appeal), appeal_id))
        return self.get_evaluation(evaluation["id"])

    def export_dataset(self, dataset_id):
        with self.connection() as conn:
            dataset = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if not dataset:
                raise DomainError("NOT_FOUND", "Dataset not found", 404)
            conversations = conn.execute("SELECT body FROM conversations WHERE dataset_id=? ORDER BY ordinal", (dataset_id,)).fetchall()
            ids = [r[0] for r in conn.execute("SELECT e.id FROM evaluations e JOIN conversations c ON c.id=e.conversation_id WHERE c.dataset_id=? ORDER BY e.created_at", (dataset_id,)).fetchall()]
        return {"schema_version": 1, "exported_at": now(), "dataset": dict(dataset), "conversations": [json.loads(r["body"]) for r in conversations], "evaluations": [self.get_evaluation(i) for i in ids]}
