"""Batch discovery and creation replay across retries, contention and restarts."""

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.errors import DomainError
from backend.evaluator import EvaluatorOutput
from backend.main import create_app
from backend.models import EvaluationResult
from backend.work_models import BatchRequest


def request_for(conversation):
    return BatchRequest(
        conversation_ids=[conversation["id"]], checklist_id="freedom-broker", checklist_version=1
    )


def test_batch_list_paginates_with_deterministic_ties_and_no_item_bodies(
    client, conversation, monkeypatch
):
    store = client.app.state.services.work_store
    monkeypatch.setattr("backend.work_store.utc_now", lambda: "2026-11-07T00:00:00+00:00")
    batches = [store.create_batch(request_for(conversation), 100) for _ in range(3)]
    expected = sorted([b.id for b in batches], reverse=True)
    first = client.get("/api/v1/evaluation-batches?limit=2&offset=0")
    assert first.status_code == 200
    data = first.json()
    assert data["total"] == 3 and data["limit"] == 2 and data["offset"] == 0
    assert [b["id"] for b in data["items"]] == expected[:2]
    second = client.get("/api/v1/evaluation-batches?limit=2&offset=2").json()
    assert [b["id"] for b in second["items"]] == expected[2:]
    assert client.get("/api/v1/evaluation-batches?offset=50").json()["items"] == []
    for batch in data["items"]:
        assert "items" not in batch and "conversation_ids" not in batch
        assert batch["total"] == batch["pending"] == 1 and not batch["completed"]
        assert (
            batch["running"] == batch["succeeded"] == batch["failed"] == batch["interrupted"] == 0
        )


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=no"])
def test_batch_list_invalid_pagination(client, query):
    assert client.get("/api/v1/evaluation-batches?" + query).status_code == 422


def test_batch_list_empty_and_latest_first(client, conversation):
    assert client.get("/api/v1/evaluation-batches").json() == {
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }
    store = client.app.state.services.work_store
    first = store.create_batch(request_for(conversation), 100)
    second = store.create_batch(request_for(conversation), 100)
    with client.app.state.repository.connection(write=True) as conn:
        conn.execute(
            "UPDATE evaluation_batches SET created_at=? WHERE id=?", ("2020-01-01", first.id)
        )
    assert client.get("/api/v1/evaluation-batches").json()["items"][0]["id"] == second.id


def test_progress_counts_include_each_state_and_completed_means_terminal(client, dataset):
    records = [deepcopy(dataset["records"][0]) for _ in range(5)]
    for index, record in enumerate(records):
        record["conversation_id"] = f"synthetic-{index}"
    imported = client.post("/api/v1/datasets", json={**dataset, "records": records}).json()
    ids = [
        c["id"]
        for c in client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()["items"]
    ]
    batch = client.app.state.services.work_store.create_batch(
        BatchRequest(conversation_ids=ids, checklist_id="freedom-broker", checklist_version=1), 100
    )
    with client.app.state.repository.connection(write=True) as conn:
        for item, status in zip(
            batch.items, ["pending", "running", "succeeded", "failed", "interrupted"], strict=True
        ):
            conn.execute("UPDATE batch_items SET status=? WHERE id=?", (status, item.id))
    summary = client.get("/api/v1/evaluation-batches").json()["items"][0]
    assert summary["total"] == 5 and not summary["completed"]
    assert all(
        summary[state] == 1
        for state in ["pending", "running", "succeeded", "failed", "interrupted"]
    )
    with client.app.state.repository.connection(write=True) as conn:
        conn.execute(
            "UPDATE batch_items SET status='interrupted' WHERE status IN ('pending','running')"
        )
    summary = client.get("/api/v1/evaluation-batches").json()["items"][0]
    assert summary["completed"] and summary["interrupted"] == 3


def test_batch_replay_survives_restart_and_disabled_ai(settings, client, conversation):
    key, request = "batch-restart", request_for(conversation)
    original = client.app.state.services.work_store.create_batch(
        request, 1, hashlib.sha256(key.encode()).hexdigest()
    )
    with client.app.state.repository.connection(write=True) as conn:
        conn.execute("UPDATE batch_items SET status='interrupted' WHERE batch_id=?", (original.id,))
    with TestClient(create_app(settings)) as restarted:
        replay = restarted.post(
            "/api/v1/evaluation-batches",
            json=request.model_dump(),
            headers={"Idempotency-Key": key},
        )
        assert replay.status_code == 201 and replay.json() == original.model_dump()
        current = restarted.get(f"/api/v1/evaluation-batches/{original.id}").json()
        assert current["completed"] and current["items"][0]["status"] == "interrupted"
        assert restarted.get("/api/v1/evaluation-batches").json()["total"] == 1
        changed = {**request.model_dump(), "checklist_version": 2}
        conflict = restarted.post(
            "/api/v1/evaluation-batches", json=changed, headers={"Idempotency-Key": key}
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        new = restarted.post(
            "/api/v1/evaluation-batches",
            json=request.model_dump(),
            headers={"Idempotency-Key": "new"},
        )
        assert new.status_code == 503 and new.json()["error"]["code"] == "AI_NOT_CONFIGURED"


def test_parallel_posts_create_one_batch_and_one_model_call(
    settings, conversation, answers, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    class Model:
        configured = True
        calls = 0

        async def evaluate(self, context):
            self.calls += 1
            entered.set()
            assert await asyncio.to_thread(release.wait, 5)
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="test-only",
                prompt_version="v1",
            )

    model = Model()
    app = create_app(replace(settings, batch_queue_limit=1), model)
    barrier = threading.Barrier(2)
    original_read = app.state.services.work_store.replay_batch

    def simultaneous_miss(*args):
        result = original_read(*args)
        barrier.wait(3)
        return result

    monkeypatch.setattr(app.state.services.work_store, "replay_batch", simultaneous_miss)
    with TestClient(app) as client:

        def post(_):
            return client.post(
                "/api/v1/evaluation-batches",
                json=request_for(conversation).model_dump(),
                headers={"Idempotency-Key": "same-batch"},
            )

        try:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(post, range(2)))
            assert all(r.status_code == 201 for r in results)
            assert results[0].json() == results[1].json()
            assert entered.wait(3) and model.calls == 1
            assert client.get("/api/v1/evaluation-batches").json()["total"] == 1
        finally:
            release.set()


def test_batch_key_is_atomic_with_batch_and_items(client, conversation):
    store = client.app.state.services.work_store
    repository = client.app.state.repository
    with repository.connection(write=True) as conn:
        conn.execute("""CREATE TRIGGER fail_batch_key BEFORE INSERT ON batch_creation_requests
                     BEGIN SELECT RAISE(ABORT,'synthetic storage failure'); END""")
    with pytest.raises(DomainError) as error:
        store.create_batch(request_for(conversation), 10, "key-hash")
    assert error.value.code == "STORAGE_ERROR"
    with repository.connection(write=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM batch_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM batch_creation_requests").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_batch_key")
    original = store.create_batch(request_for(conversation), 1, "key-hash")
    assert store.create_batch(request_for(conversation), 1, "key-hash") == original
    with pytest.raises(DomainError) as full:
        store.create_batch(request_for(conversation), 1, "different-key")
    assert full.value.code == "BATCH_QUEUE_FULL"


def test_no_key_still_creates_new_batches_and_keys_are_not_exposed(client, conversation):
    store = client.app.state.services.work_store
    request = request_for(conversation)
    first, second = store.create_batch(request, 10), store.create_batch(request, 10)
    assert first.id != second.id
    plain_key = "private-request-key"
    hashed = hashlib.sha256(plain_key.encode()).hexdigest()
    keyed = store.create_batch(request, 10, hashed)
    repository = client.app.state.repository
    repository.initialize()
    assert store.replay_batch(request, hashed) == keyed
    listed = client.get("/api/v1/evaluation-batches")
    assert listed.json()["total"] == 3
    assert plain_key not in listed.text and hashed not in listed.text
    with repository.connection() as conn:
        assert conn.execute("SELECT key_hash FROM batch_creation_requests").fetchone()[0] == hashed


@pytest.mark.parametrize("key", ["", "has spaces", "x" * 129])
def test_invalid_batch_idempotency_key_is_rejected_before_creation(client, conversation, key):
    response = client.post(
        "/api/v1/evaluation-batches",
        json=request_for(conversation).model_dump(),
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 422
    assert client.get("/api/v1/evaluation-batches").json()["total"] == 0


def test_batch_shutdown_exits_even_if_a_wait_swallows_cancellation(client):
    from backend.batches import BatchService, ExecutionRuntime

    async def scenario():
        entered = asyncio.Event()

        class RacyWake:
            waits = 0

            def clear(self):
                pass

            def set(self):
                pass

            async def wait(self):
                self.waits += 1
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if self.waits == 1:
                        return True  # Simulate completion winning the cancellation race.
                    raise

        class Owners:
            closed = False

            def close_owner(self, owner, now):
                self.closed = True

        services = client.app.state.services
        consumer = BatchService(services.coordinator, services.work_store, 100)
        wake = RacyWake()
        consumer.wake = wake
        owners = Owners()
        runtime = ExecutionRuntime(consumer, owners, "test-owner", 30)
        runtime.tasks = [asyncio.create_task(consumer.consume())]
        await asyncio.wait_for(entered.wait(), 1)
        async with asyncio.timeout(1):
            await runtime.close()
        assert owners.closed and runtime.tasks[0].done() and wake.waits == 1

    asyncio.run(scenario())
