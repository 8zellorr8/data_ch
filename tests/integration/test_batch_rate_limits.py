"""Synthetic models/HTTP only: durable retries, no duplicate scores or quota spend."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from time import monotonic, sleep, time

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.gemini import GeminiEvaluator
from backend.backup import create_backup, restore_backup
from backend.db import Repository
from backend.errors import DomainError
from backend.evaluator import EvaluatorOutput, ProviderDeferred
from backend.main import create_app
from backend.models import EvaluationResult
from backend.rate_limits import RequestLimiter, SQLiteRateStore
from backend.work_models import BatchRequest, ProviderUsage, UsageSelection


def import_ids(client, dataset, count=3):
    records = []
    for i in range(count):
        record = deepcopy(dataset["records"][0])
        record["conversation_id"] = f"retry-synthetic-{i}"
        records.append(record)
    imported = client.post("/api/v1/datasets", json={**dataset, "records": records}).json()
    return [
        c["id"]
        for c in client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()["items"]
    ]


def request(ids):
    return {"conversation_ids": ids, "checklist_id": "freedom-broker", "checklist_version": 1}


def wait_batch(client, batch_id, predicate):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        batch = client.get(f"/api/v1/evaluation-batches/{batch_id}").json()
        if predicate(batch):
            return batch
        sleep(0.01)
    pytest.fail(f"Batch did not reach expected state: {batch}")


class GoodModel:
    configured = True

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    async def evaluate(self, context):
        self.calls.append(context.conversation.id)
        return EvaluatorOutput(
            EvaluationResult.model_validate(self.answers),
            "synthetic-test-model",
            "test-v1",
            usage=ProviderUsage(model="synthetic-test-model", calls=1),
        )


def test_temporary_429_retries_only_failed_item_and_keeps_attempt_usage(settings, dataset, answers):
    class OnceLimited(GoodModel):
        async def evaluate(self, context):
            if len(self.calls) == 1:
                self.calls.append(context.conversation.id)
                raise ProviderDeferred(
                    time() + 0.05, usage=ProviderUsage(model="synthetic-test-model", calls=1)
                )
            return await super().evaluate(context)

    model = OnceLimited(answers)
    with TestClient(create_app(settings, model)) as client:
        ids = import_ids(client, dataset)
        batch = client.post(
            "/api/v1/evaluation-batches",
            json=request(ids),
            headers={"Idempotency-Key": "retry-batch"},
        ).json()
        result = wait_batch(client, batch["id"], lambda b: b["completed"])
        assert [i["status"] for i in result["items"]] == ["succeeded"] * 3
        assert model.calls == [ids[0], ids[1], ids[1], ids[2]]
        assert result["items"][1]["rate_limit_retries"] == 1
        assert result["items"][1]["retry_at"] is None
        report = client.post("/api/v1/reports/usage", json={"batch_id": batch["id"]}).json()
        assert report["known_calls"] == 4 and len(report["rows"]) == 4
        assert report["missing_ids"] == []
        assert client.post(f"/api/v1/evaluation-batches/{batch['id']}/resume").json() == result
        client.post(
            "/api/v1/evaluation-batches",
            json=request(ids),
            headers={"Idempotency-Key": "retry-batch"},
        )
        assert model.calls == [ids[0], ids[1], ids[1], ids[2]]
        for cid in ids:
            assert client.get(f"/api/v1/conversations/{cid}/evaluations").json()["total"] == 1


def test_gemini_daily_cooldown_survives_restart_and_then_continues(settings, dataset, answers):
    calls = []

    def provider(req):
        calls.append(req)
        if len(calls) == 2:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "53045s",
                            },
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [
                                    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                                ],
                            },
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answers)}]}}
                ]
            },
        )

    def evaluator():
        return GeminiEvaluator(
            "test-key",
            model="gemini-3.6-flash",
            client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
            request_gate=RequestLimiter(
                SQLiteRateStore(Repository(settings.database_path)), requests_per_minute=60000
            ),
        )

    with TestClient(create_app(settings, evaluator())) as client:
        ids = import_ids(client, dataset)
        batch = client.post("/api/v1/evaluation-batches", json=request(ids)).json()
        pending = wait_batch(client, batch["id"], lambda b: b["items"][1]["retry_at"] is not None)
        original_id = pending["items"][0]["evaluation_id"]
        assert pending["items"][0]["status"] == "succeeded"
        assert pending["items"][1]["status"] == "pending"
        assert pending["items"][1]["error_code"] == "AI_QUOTA_EXHAUSTED"
        assert pending["items"][1]["retry_at"] > time() + 53000
        # A separate single evaluation obeys the same cooldown, without another HTTP call.
        blocked = client.post("/api/v1/evaluations", json={"conversation_id": ids[2]})
        assert blocked.status_code == 502 and blocked.json()["error"]["code"] == "AI_UNAVAILABLE"
        assert int(blocked.headers["Retry-After"]) >= 53000
        usage = client.post(
            "/api/v1/reports/usage", json={"execution_ids": [blocked.headers["X-Execution-ID"]]}
        ).json()
        assert usage["known_calls"] == 0 and usage["calls_complete"]
        assert usage["rows"][0]["execution"]["provider_not_called"]
        assert len(calls) == 2

    with TestClient(create_app(settings, evaluator())) as restarted:
        again = wait_batch(restarted, batch["id"], lambda b: b["items"][2]["retry_at"] is not None)
        assert again["items"][0]["evaluation_id"] == original_id and len(calls) == 2
        assert restarted.post(f"/api/v1/evaluation-batches/{batch['id']}/resume").json() == again
        assert len(calls) == 2

    # Simulate expiry of a 15-hour deadline without spending real wall time.
    repo = Repository(settings.database_path)
    with repo.connection(write=True) as conn:
        conn.execute("UPDATE provider_limits SET blocked_until=0,next_at=0")
        conn.execute("UPDATE batch_retry_state SET retry_at=0")
    with TestClient(create_app(settings, evaluator())) as restarted:
        final = wait_batch(restarted, batch["id"], lambda b: b["completed"])
        assert all(i["status"] == "succeeded" for i in final["items"])
        assert final["items"][0]["evaluation_id"] == original_id
        assert len(calls) == 4  # first success + refused second + second/third success
        report = restarted.post("/api/v1/reports/usage", json={"batch_id": batch["id"]}).json()
        assert report["known_calls"] == 4 and report["missing_ids"] == []
        assert len(report["rows"]) == 5  # includes the known-zero local cooldown after restart


def test_resume_exhausted_budget_is_repeatable_and_ignores_unsafe_states(client, dataset):
    ids = import_ids(client, dataset, 6)
    store = client.app.state.services.work_store
    batch = store.create_batch(BatchRequest(**request(ids)), 100)
    states = [
        ("failed", "AI_RATE_LIMITED"),
        ("failed", "AI_QUOTA_EXHAUSTED"),
        ("succeeded", None),
        ("interrupted", "EVALUATION_INTERRUPTED"),
        ("interrupted", "BATCH_CANCELLED_BEFORE_START"),
        ("failed", "AI_TIMEOUT"),
    ]
    with client.app.state.repository.connection(write=True) as conn:
        for item, (status, code) in zip(batch.items, states, strict=True):
            conn.execute(
                "UPDATE batch_items SET status=?,error_code=? WHERE id=?", (status, code, item.id)
            )
        conn.execute(
            "INSERT INTO batch_retry_state VALUES (?,?,?)", (batch.items[0].id, time() + 1000, 4)
        )
    url = f"/api/v1/evaluation-batches/{batch.id}/resume"
    before = store.get_batch(batch.id)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post(url).json(), range(2)))
    assert results[0] == results[1]
    after = results[0]
    assert [i["status"] for i in after["items"][:2]] == ["pending", "pending"]
    assert after["items"][0]["retry_at"] == before.items[0].retry_at
    assert after["items"][0]["rate_limit_retries"] == 0
    assert after["items"][2:] == [i.model_dump() for i in before.items[2:]]
    assert client.post("/api/v1/evaluation-batches/missing/resume").status_code == 404
    assert client.post(f"/api/v1/evaluation-batches/{batch.id}/cancel").status_code == 200
    assert client.post(url).json()["items"][0]["status"] == "interrupted"


def test_retry_budget_stops_repeated_refusals_and_history_remains(client, conversation):
    store = client.app.state.services.work_store
    store.register_owner("retry-owner", time(), 600)
    batch = store.create_batch(BatchRequest(**request([conversation["id"]])), 100)
    for attempt in range(4):
        item = store.claim("retry-owner", time())
        execution = store.reserve(
            "retry-owner", time(), "ai", None, "same", conversation["id"], item.id
        )
        store.fail(
            execution.id,
            "AI_RATE_LIMITED",
            10,
            ProviderUsage(model="synthetic", calls=1),
            retry_at=0,
            max_retries=3,
        )
        state = store.get_batch(batch.id).items[0]
        assert state.rate_limit_retries == attempt + 1
        assert state.status == ("pending" if attempt < 3 else "failed")
    assert store.claim("retry-owner", time()) is None
    records, missing, pending = store.get_usage(UsageSelection(batch_id=batch.id))
    assert len(records) == 4 and missing == [] and pending == 0
    store.resume_batch(batch.id, 100)
    assert store.claim("retry-owner", time()).id == item.id


def test_reschedule_and_failure_log_are_one_transaction(client, conversation):
    store = client.app.state.services.work_store
    store.register_owner("atomic-retry", time(), 600)
    batch = store.create_batch(BatchRequest(**request([conversation["id"]])), 100)
    item = store.claim("atomic-retry", time())
    execution = store.reserve(
        "atomic-retry", time(), "ai", None, "same", conversation["id"], item.id
    )
    with client.app.state.repository.connection(write=True) as conn:
        conn.execute("""CREATE TRIGGER fail_retry BEFORE INSERT ON batch_retry_state
                        BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
    with pytest.raises(DomainError):
        store.fail(execution.id, "AI_RATE_LIMITED", 0, None, retry_at=time() + 60, max_retries=3)
    assert store.get_batch(batch.id).items[0].status == "running"
    with client.app.state.repository.connection() as conn:
        assert (
            conn.execute(
                "SELECT status FROM evaluation_executions WHERE id=?", (execution.id,)
            ).fetchone()[0]
            == "running"
        )


def test_resume_queue_limit_is_atomic(client, conversation):
    store = client.app.state.services.work_store
    batches = [
        store.create_batch(BatchRequest(**request([conversation["id"]])), 100) for _ in range(2)
    ]
    with client.app.state.repository.connection(write=True) as conn:
        conn.execute(
            "UPDATE batch_items SET status='failed',error_code='AI_RATE_LIMITED' WHERE batch_id=?",
            (batches[1].id,),
        )
    with pytest.raises(DomainError) as caught:
        store.resume_batch(batches[1].id, 1)
    assert caught.value.code == "BATCH_QUEUE_FULL"
    assert store.get_batch(batches[1].id).items[0].status == "failed"


def test_restore_never_resumes_deferred_work(settings, client, conversation, answers, tmp_path):
    store = client.app.state.services.work_store
    store.register_owner("backup-retry", time(), 600)
    batch = store.create_batch(BatchRequest(**request([conversation["id"]])), 100)
    item = store.claim("backup-retry", time())
    execution = store.reserve(
        "backup-retry", time(), "ai", None, "same", conversation["id"], item.id
    )
    store.fail(
        execution.id,
        "AI_RATE_LIMITED",
        0,
        ProviderUsage(model="synthetic", calls=1),
        retry_at=0,
        max_retries=3,
    )
    backup, restored = tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3"
    create_backup(settings.database_path, backup)
    restore_backup(backup, restored)
    model = GoodModel(answers)
    with TestClient(create_app(replace(settings, database_path=restored), model)) as restarted:
        result = restarted.post(f"/api/v1/evaluation-batches/{batch.id}/resume").json()
        assert result["items"][0]["status"] == "interrupted"
        assert result["items"][0]["error_code"] == "RESTORE_REQUIRES_REVIEW"
    assert model.calls == []


def test_late_failed_attempt_cannot_overwrite_another_running_attempt(client, conversation):
    store = client.app.state.services.work_store
    store.register_owner("repeat-owner", time(), 600)
    batch = store.create_batch(BatchRequest(**request([conversation["id"]])), 100)
    item = store.claim("repeat-owner", time())
    first = store.reserve("repeat-owner", time(), "ai", None, "same", conversation["id"], item.id)
    store.fail(first.id, "AI_RATE_LIMITED", 0, None, retry_at=0, max_retries=3)
    item = store.claim("repeat-owner", time())
    second = store.reserve("repeat-owner", time(), "ai", None, "same", conversation["id"], item.id)
    store.fail(first.id, "AI_RATE_LIMITED", 0, None, retry_at=0, max_retries=3)
    assert store.get_batch(batch.id).items[0].status == "running"
    assert first.id != second.id


def test_item_cannot_have_two_active_attempts(client, conversation):
    store = client.app.state.services.work_store
    store.register_owner("single-attempt", time(), 600)
    store.create_batch(BatchRequest(**request([conversation["id"]])), 100)
    item = store.claim("single-attempt", time())
    store.reserve("single-attempt", time(), "ai", None, "first", conversation["id"], item.id)
    with pytest.raises(DomainError):
        store.reserve("single-attempt", time(), "ai", None, "second", conversation["id"], item.id)


def test_ambiguous_attempt_is_not_resumed_by_queue_or_endpoint(settings, dataset):
    class AmbiguousModel:
        configured = True
        calls = 0

        async def evaluate(self, context):
            self.calls += 1
            raise ProviderDeferred(
                time() + 1, safe_to_retry=False, usage=ProviderUsage(model="synthetic", calls=2)
            )

    model = AmbiguousModel()
    with TestClient(create_app(settings, model)) as client:
        ids = import_ids(client, dataset, 1)
        batch = client.post("/api/v1/evaluation-batches", json=request(ids)).json()
        failed = wait_batch(client, batch["id"], lambda b: b["completed"])
        assert failed["items"][0]["status"] == "failed"
        assert failed["items"][0]["error_code"] == "AI_UNAVAILABLE"
        assert client.post(f"/api/v1/evaluation-batches/{batch['id']}/resume").json() == failed
        assert model.calls == 1
