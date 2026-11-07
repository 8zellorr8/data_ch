"""Behavioral tests for durable work, cancellation, accounting and API compatibility."""

import asyncio
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from time import monotonic, time

import pytest
from fastapi.testclient import TestClient

from backend.errors import DomainError
from backend.evaluator import EvaluatorFailure, EvaluatorOutput
from backend.execution import AdmissionLimit, EvaluationCoordinator, request_fingerprint
from backend.main import create_app
from backend.models import EvaluationRequest, EvaluationResult
from backend.observability import request_id_context
from backend.services import EvaluationService
from backend.work_models import BatchRequest, ProviderUsage, UsageSelection
from backend.work_store import WorkStore


class ValidEvaluator:
    configured = True

    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    async def evaluate(self, context):
        self.calls += 1
        return EvaluatorOutput(
            result=EvaluationResult.model_validate(self.answers),
            model="test-only",
            prompt_version="v1",
            input_tokens=100,
            output_tokens=50,
            usage=ProviderUsage(
                model="test-only", calls=1, input_tokens=100, output_tokens=50, complete=True
            ),
        )


def post_ai(client, conversation, key=None):
    return client.post(
        "/api/v1/evaluations",
        json={"conversation_id": conversation["id"]},
        headers={"Idempotency-Key": key} if key else {},
    )


def test_manual_replay_survives_appeal_and_restart(settings, client, conversation, answers):
    payload = {"conversation_id": conversation["id"], "author": "QA", "result": answers}
    headers = {"Idempotency-Key": "manual-1"}
    original = client.post("/api/v1/evaluations/manual", json=payload, headers=headers)
    assert original.status_code == 201
    evaluation_id = original.json()["id"]
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation_id}/appeals",
        json={"criterion_id": "contact", "author": "QA", "comment": "Review"},
    ).json()
    changed = client.post(
        f"/api/v1/appeals/{appeal['id']}/decision",
        json={
            "expected_revision": 1,
            "value": "no",
            "reason": "Correction",
            "reviewer": "QA",
            "evidence_kind": "missing",
            "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
        },
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    with TestClient(create_app(settings)) as restarted:
        replay = restarted.post("/api/v1/evaluations/manual", json=payload, headers=headers)
        assert replay.status_code == 201 and replay.json() == original.json()
        conflict = restarted.post(
            "/api/v1/evaluations/manual", json={**payload, "author": "Different"}, headers=headers
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert (
            restarted.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()["total"]
            == 1
        )


def test_concurrent_ai_duplicate_and_busy_do_not_call_twice(settings, conversation, answers):
    entered, release = threading.Event(), threading.Event()

    class BlockingEvaluator(ValidEvaluator):
        async def evaluate(self, context):
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            return await super().evaluate(context)

    model = BlockingEvaluator(answers)
    with TestClient(create_app(replace(settings, ai_concurrency=1), model)) as client:
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(post_ai, client, conversation, "same")
            try:
                assert entered.wait(3)
                duplicate = post_ai(client, conversation, "same")
                assert duplicate.status_code == 409
                assert duplicate.json()["error"]["code"] == "EVALUATION_IN_PROGRESS"
                busy = post_ai(client, conversation, "different")
                assert busy.status_code == 503 and busy.json()["error"]["code"] == "AI_BUSY"
            finally:
                release.set()
            original = first.result(timeout=5)
        assert original.status_code == 201
        replay = post_ai(client, conversation, "same")
        assert replay.json() == original.json() and model.calls == 1
        assert replay.headers["x-execution-id"] == original.headers["x-execution-id"]
        assert post_ai(client, conversation, "different").status_code == 201
        assert model.calls == 2


def test_success_and_idempotency_rollback_together(settings, conversation, answers):
    model = ValidEvaluator(answers)
    with TestClient(create_app(settings, model)) as client:
        store = client.app.state.repository
        with store.connection(write=True) as conn:
            conn.execute("""CREATE TRIGGER fail_execution BEFORE UPDATE OF evaluation_id
                ON evaluation_executions WHEN NEW.evaluation_id IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        failed = post_ai(client, conversation, "atomic")
        assert failed.status_code == 500 and failed.json()["error"]["code"] == "STORAGE_ERROR"
        assert store.list_evaluations(conversation["id"]).total == 0
        with store.connection() as conn:
            row = conn.execute(
                "SELECT status,evaluation_id,response FROM evaluation_executions"
            ).fetchone()
            assert tuple(row) == ("failed", None, None)
        assert (
            post_ai(client, conversation, "atomic").json()["error"]["code"] == "EVALUATION_FAILED"
        )
        assert model.calls == 1


def test_failed_provider_usage_and_all_retries_are_reported(settings, conversation):
    class FailedEvaluator:
        configured = True
        calls = 0

        async def evaluate(self, context):
            self.calls += 1
            raise EvaluatorFailure(
                "AI_UNAVAILABLE",
                "Provider failed",
                ProviderUsage(
                    model="test-only", calls=3, input_tokens=300, output_tokens=20, complete=True
                ),
            )

    model = FailedEvaluator()
    with TestClient(create_app(settings, model)) as client:
        failed = post_ai(client, conversation, "failed")
        assert failed.status_code == 502
        report = client.post(
            "/api/v1/reports/usage", json={"execution_ids": [failed.headers["x-execution-id"]]}
        ).json()
        assert report["known_calls"] == 3 and report["calls_complete"] is True
        assert report["known_input_tokens"] == 300 and report["complete"] is False
        assert report["rows"][0]["limitations"] == ["TARIFF_UNKNOWN"]
        assert post_ai(client, conversation, "failed").status_code == 409
        assert model.calls == 1


def test_expired_owner_recovery_preserves_pending_and_fences_late_commit(
    client, conversation, evaluation
):
    from backend.models import EvaluationRecord

    store = WorkStore(client.app.state.repository)
    store.register_owner("old-owner", 100, 10)
    batch = store.create_batch(
        BatchRequest(
            conversation_ids=[conversation["id"]],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    item = store.claim("old-owner", 101)
    reservation = store.reserve(
        "old-owner", 101, "ai", "key", "fingerprint", conversation["id"], item.id
    )
    store.register_owner("other-owner", 105, 30)
    assert store.get_batch(batch.id).items[0].status == "running"
    store.heartbeat("other-owner", 111, 30)
    assert store.get_batch(batch.id).items[0].status == "interrupted"
    with pytest.raises(DomainError, match="Previous execution"):
        store.replay("ai", "key", "fingerprint")
    with pytest.raises(DomainError, match="lease expired"):
        store.complete(
            reservation.id, "old-owner", 111, EvaluationRecord.model_validate(evaluation), 1, None
        )
    pending = store.create_batch(
        BatchRequest(
            conversation_ids=[conversation["id"]],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    store.heartbeat("other-owner", 115, 30)
    assert store.get_batch(pending.id).items[0].status == "pending"
    assert store.claim("other-owner", 115).batch_id == pending.id


def test_two_owners_claim_one_item_once(client, conversation):
    store = WorkStore(client.app.state.repository)
    for owner in ("a", "b"):
        store.register_owner(owner, 100, 30)
    store.create_batch(
        BatchRequest(
            conversation_ids=[conversation["id"]],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    barrier = threading.Barrier(2)

    def claim(owner):
        barrier.wait(3)
        return store.claim(owner, 101)

    with ThreadPoolExecutor(2) as pool:
        claimed = list(pool.map(claim, ["a", "b"]))
    assert sum(item is not None for item in claimed) == 1


def test_cancelled_evaluation_releases_slot_and_persists_interrupted(client, conversation, answers):
    store = WorkStore(client.app.state.repository)
    store.register_owner("cancel-test", time(), 60)

    async def scenario():
        entered = asyncio.Event()

        class Model(ValidEvaluator):
            async def evaluate(self, context):
                if not entered.is_set():
                    entered.set()
                    await asyncio.Event().wait()
                return await super().evaluate(context)

        coordinator = EvaluationCoordinator(
            EvaluationService(client.app.state.repository, Model(answers)),
            store,
            "cancel-test",
            AdmissionLimit(1),
        )
        request = EvaluationRequest(conversation_id=conversation["id"])
        task = asyncio.create_task(coordinator.evaluate(request, "cancel"))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(DomainError) as error:
            await coordinator.evaluate(request, "cancel")
        assert error.value.code == "EVALUATION_INTERRUPTED"
        assert (await coordinator.evaluate(request, "next")).origin == "ai"

    asyncio.run(scenario())


def test_cancel_during_commit_finishes_atomically(client, conversation, answers, monkeypatch):
    store = WorkStore(client.app.state.repository)
    store.register_owner("commit-test", time(), 60)
    entered, release = threading.Event(), threading.Event()
    original = store.complete

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(store, "complete", blocked)

    async def scenario():
        coordinator = EvaluationCoordinator(
            EvaluationService(client.app.state.repository, ValidEvaluator(answers)),
            store,
            "commit-test",
            AdmissionLimit(1),
        )
        request = EvaluationRequest(conversation_id=conversation["id"])
        task = asyncio.create_task(coordinator.evaluate(request, "during-commit"))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        replay = await coordinator.evaluate(request, "during-commit")
        assert replay.origin == "ai"
        assert client.app.state.repository.list_evaluations(conversation["id"]).total == 1

    asyncio.run(scenario())


def wait_batch(client, batch_id):
    deadline = monotonic() + 5
    while monotonic() < deadline:
        result = client.get(f"/api/v1/evaluation-batches/{batch_id}")
        assert result.status_code == 200
        if result.json()["completed"]:
            return result.json()
        threading.Event().wait(0.01)
    pytest.fail("Batch did not complete")


def test_batch_partial_failure_and_usage(settings, dataset, answers):
    records = []
    for i in range(3):
        item = deepcopy(dataset["records"][0])
        item["conversation_id"] = f"synthetic-{i}"
        records.append(item)

    class Model(ValidEvaluator):
        async def evaluate(self, context):
            if context.conversation.conversation_id == "synthetic-1":
                raise DomainError("AI_UNAVAILABLE", "Synthetic provider failure")
            return await super().evaluate(context)

    model = Model(answers)
    with TestClient(create_app(settings, model)) as client:
        imported = client.post("/api/v1/datasets", json={**dataset, "records": records}).json()
        ids = [
            c["id"]
            for c in client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()["items"]
        ]
        response = client.post(
            "/api/v1/evaluation-batches",
            json={
                "conversation_ids": ids,
                "checklist_id": "freedom-broker",
                "checklist_version": 1,
            },
        )
        assert response.status_code == 201
        batch = wait_batch(client, response.json()["id"])
        assert [i["status"] for i in batch["items"]] == ["succeeded", "failed", "succeeded"]
        assert model.calls == 2
        report = client.post("/api/v1/reports/usage", json={"batch_id": batch["id"]}).json()
        assert len(report["rows"]) == 3 and report["complete"] is False
        assert report["calls_complete"] is False and report["missing_ids"] == []
        assert client.get(f"/api/v1/datasets/{imported['id']}/export").json()["evaluations"]


def test_batch_validation_no_ai_and_queue_bound(client, conversation):
    payload = {
        "conversation_ids": [conversation["id"]],
        "checklist_id": "freedom-broker",
        "checklist_version": 1,
    }
    assert client.post("/api/v1/evaluation-batches", json=payload).status_code == 503
    assert (
        client.post(
            "/api/v1/evaluation-batches",
            json={**payload, "conversation_ids": [conversation["id"]] * 2},
        ).status_code
        == 422
    )
    store = WorkStore(client.app.state.repository)
    store.create_batch(BatchRequest.model_validate(payload), 1)
    with pytest.raises(DomainError) as error:
        store.create_batch(BatchRequest.model_validate(payload), 1)
    assert error.value.code == "BATCH_QUEUE_FULL"


def test_request_ids_cors_and_private_data_redaction(client, caplog):
    caplog.set_level(logging.INFO)
    response = client.get(
        "/api/v1/conversations/private-customer?sig=private-key",
        headers={
            "X-Request-ID": "trace-123",
            "Authorization": "Bearer private-auth",
            "Origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 404 and response.headers["x-request-id"] == "trace-123"
    assert "X-Request-ID" in response.headers["access-control-expose-headers"]
    records = [r.getMessage() for r in caplog.records if r.name == "backend.requests"]
    assert any(
        "route=/api/v1/conversations/{conversation_id}" in r and "code=NOT_FOUND" in r
        for r in records
    )
    assert all("private-" not in r for r in records)
    assert "private-key" not in caplog.text and "private-auth" not in caplog.text
    invalid = client.get("/health", headers={"X-Request-ID": "bad id\tvalue"})
    assert invalid.headers["x-request-id"] != "bad id\tvalue"
    preflight = client.options(
        "/api/v1/evaluations",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Idempotency-Key,X-Request-ID,Content-Type",
        },
    )
    assert preflight.status_code == 200 and preflight.headers["x-request-id"]


def test_request_context_isolated_between_threads(client, monkeypatch):
    barrier = threading.Barrier(2)
    observed = []

    def read():
        current = request_id_context.get()
        barrier.wait(3)
        observed.append((current, request_id_context.get()))
        return []

    monkeypatch.setattr(client.app.state.repository, "list_datasets", read)

    def get(request_id):
        return client.get("/api/v1/datasets", headers={"X-Request-ID": request_id})

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(get, ["request-a", "request-b"]))
    assert {r.headers["x-request-id"] for r in responses} == {"request-a", "request-b"}
    assert set(observed) == {("request-a", "request-a"), ("request-b", "request-b")}
    assert request_id_context.get() is None


def test_idempotency_reservation_is_atomic(client, conversation):
    store = WorkStore(client.app.state.repository)
    store.register_owner("atomic-owner", time(), 60)
    request = EvaluationRequest(conversation_id=conversation["id"])
    barrier = threading.Barrier(2)

    def reserve(_):
        barrier.wait(3)
        try:
            return store.reserve(
                "atomic-owner",
                time(),
                "ai",
                hashlib.sha256(b"same").hexdigest(),
                request_fingerprint(request),
                conversation["id"],
            ).id
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, range(2)))
    assert results.count("EVALUATION_IN_PROGRESS") == 1
    with client.app.state.repository.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_executions").fetchone()[0] == 1


def test_legacy_evaluation_usage_is_marked_incomplete(client, conversation, answers):
    service = EvaluationService(client.app.state.repository, ValidEvaluator(answers))
    record = asyncio.run(service.evaluate(EvaluationRequest(conversation_id=conversation["id"])))
    report = client.post("/api/v1/reports/usage", json={"evaluation_ids": [record.id]}).json()
    assert report["known_calls"] == 1 and report["calls_complete"] is False
    assert report["rows"][0]["execution"]["id"] == "legacy:" + record.id


def test_usage_selection_does_not_double_count(client):
    assert (
        client.post("/api/v1/reports/usage", json={"evaluation_ids": ["a", "a"]}).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/reports/usage", json={"evaluation_ids": ["a"], "batch_id": "b"}
        ).status_code
        == 422
    )
    report = client.post("/api/v1/reports/usage", json={"execution_ids": ["unknown"]}).json()
    assert report["complete"] is False and report["missing_ids"] == ["unknown"]


def test_timeout_and_invalid_result_release_slot_and_keep_usage(settings, conversation, answers):
    class Model(ValidEvaluator):
        phase = 0

        async def evaluate(self, context):
            self.phase += 1
            if self.phase == 1:
                await asyncio.Event().wait()
            output = await super().evaluate(context)
            if self.phase == 2:
                output.result.answers[0].evidence[0].quote = "Never said"
            return output

    model = Model(answers)
    with TestClient(
        create_app(replace(settings, ai_concurrency=1, evaluation_timeout=0.03), model)
    ) as client:
        assert post_ai(client, conversation, "timeout").status_code == 504
        invalid = post_ai(client, conversation, "invalid")
        assert invalid.status_code == 502 and invalid.json()["error"]["code"] == "AI_INVALID_RESULT"
        report = client.post(
            "/api/v1/reports/usage", json={"execution_ids": [invalid.headers["x-execution-id"]]}
        ).json()
        assert report["known_input_tokens"] == 100 and report["known_calls"] == 1
        assert post_ai(client, conversation, "valid").status_code == 201


def test_batch_and_http_share_limit(settings, conversation, answers, monkeypatch):
    entered, release, batch_busy = threading.Event(), threading.Event(), threading.Event()

    class Model(ValidEvaluator):
        active, peak = 0, 0

        async def evaluate(self, context):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if not entered.is_set():
                    entered.set()
                    assert await asyncio.to_thread(release.wait, 5)
                return await super().evaluate(context)
            finally:
                self.active -= 1

    model = Model(answers)
    app = create_app(replace(settings, ai_concurrency=1), model)
    original_finish = app.state.services.work_store.finish_item

    def finish(*args, **kwargs):
        result = original_finish(*args, **kwargs)
        if kwargs.get("retry_busy"):
            batch_busy.set()
        return result

    monkeypatch.setattr(app.state.services.work_store, "finish_item", finish)
    with TestClient(app) as client:
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(post_ai, client, conversation, "first")
            try:
                assert entered.wait(3)
                batch = client.post(
                    "/api/v1/evaluation-batches",
                    json={
                        "conversation_ids": [conversation["id"]],
                        "checklist_id": "freedom-broker",
                        "checklist_version": 1,
                    },
                ).json()
                assert batch_busy.wait(3)
                assert model.peak == 1
            finally:
                release.set()
            assert first.result(3).status_code == 201
        assert wait_batch(client, batch["id"])["items"][0]["status"] == "succeeded"
        assert model.peak == 1 and model.calls == 2


def test_pending_batch_resumes_after_restart(settings, client, conversation, answers):
    # An unconfigured process must not consume pending work from a previous run.
    batch = client.app.state.services.work_store.create_batch(
        BatchRequest(
            conversation_ids=[conversation["id"]],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    model = ValidEvaluator(answers)
    with TestClient(create_app(settings, model)) as restarted:
        result = wait_batch(restarted, batch.id)
        assert result["completed"] and result["items"][0]["status"] == "succeeded"
    assert model.calls == 1


def test_benchmark_cli_reads_explicit_pair_without_model_call(
    settings, conversation, answers, tmp_path
):
    import json
    import subprocess
    import sys
    from pathlib import Path

    model = ValidEvaluator(answers)
    with TestClient(create_app(settings, model)) as client:
        manual = client.post(
            "/api/v1/evaluations/manual",
            json={"conversation_id": conversation["id"], "author": "QA", "result": answers},
        ).json()
        ai = post_ai(client, conversation).json()
        selection = tmp_path / "manifest.json"
        output = tmp_path / "quality.json"
        selection.write_text(
            json.dumps(
                {
                    "pairs": [
                        {"reference_id": manual["id"], "reference_revision": 1, "ai_id": ai["id"]}
                    ]
                }
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "backend.benchmark",
                "--database",
                str(settings.database_path),
                "--manifest",
                str(selection),
                "--output",
                str(output),
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        report = json.loads(output.read_text(encoding="utf-8"))
        assert report["compared"] == 1 and report["coverage"] == 1
        assert float(report["mae"]) == 0 and model.calls == 1
        assert "Compared: 1/1" in completed.stdout


@pytest.mark.parametrize("mode", ["validation", "large", "unexpected"])
def test_request_id_on_errors(client, monkeypatch, mode):
    if mode == "validation":
        response = client.post("/api/v1/evaluations", json={})
        assert response.status_code == 422
    elif mode == "large":
        response = client.post("/api/v1/datasets", content=b"x" * (8 * 1024 * 1024 + 1))
        assert response.status_code == 413
    else:

        def broken():
            raise RuntimeError("private-details")

        monkeypatch.setattr(client.app.state.repository, "list_datasets", broken)
        response = client.get("/api/v1/datasets")
        assert response.status_code == 500
    assert response.headers["x-request-id"]


def test_shutdown_interruption_does_not_rerun_model(settings, conversation, answers):
    entered = threading.Event()

    class Model(ValidEvaluator):
        async def evaluate(self, context):
            entered.set()
            await asyncio.Event().wait()

    with TestClient(create_app(settings, Model(answers))) as client:
        batch = client.post(
            "/api/v1/evaluation-batches",
            json={
                "conversation_ids": [conversation["id"]],
                "checklist_id": "freedom-broker",
                "checklist_version": 1,
            },
        ).json()
        assert entered.wait(3)
    next_model = ValidEvaluator(answers)
    with TestClient(create_app(settings, next_model)) as restarted:
        result = restarted.get(f"/api/v1/evaluation-batches/{batch['id']}").json()
        assert result["items"][0]["status"] == "interrupted" and result["completed"]
        assert next_model.calls == 0


def test_expired_heartbeat_commits_recovery_and_preserves_late_usage(client, conversation):
    store = WorkStore(client.app.state.repository)
    store.register_owner("expired", 100, 10)
    reservation = store.reserve("expired", 101, "ai", None, "test", conversation["id"])
    with pytest.raises(DomainError) as error:
        store.heartbeat("expired", 111, 10)
    assert error.value.code == "EVALUATION_INTERRUPTED"
    known = ProviderUsage(
        model="test-only", calls=2, input_tokens=30, output_tokens=10, complete=True
    )
    store.fail(reservation.id, "AI_UNAVAILABLE", 100, known)
    records, missing, _ = store.get_usage(UsageSelection(execution_ids=[reservation.id]))
    assert not missing and records[0].status == "interrupted"
    assert records[0].usage == known


def test_repeated_cancellation_waits_for_database_thread():
    from backend.async_utils import durable_thread

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def write():
        entered.set()
        assert release.wait(5)
        finished.set()

    async def scenario():
        task = asyncio.create_task(durable_thread(write))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())
