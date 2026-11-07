import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from time import monotonic, sleep, time

import pytest
from fastapi.testclient import TestClient

from backend.evaluator import EvaluatorOutput
from backend.main import create_app
from backend.models import EvaluationResult
from backend.work_models import BatchRequest
from backend.work_store import WorkStore


def make_batch(client, ids):
    return client.app.state.services.work_store.create_batch(
        BatchRequest(conversation_ids=ids, checklist_id="freedom-broker", checklist_version=1), 100
    )


def test_cancel_only_pending_is_repeatable_and_unknown_is_404(client, dataset):
    records = [deepcopy(dataset["records"][0]) for _ in range(5)]
    for i, record in enumerate(records):
        record["conversation_id"] = f"synthetic-{i}"
    imported = client.post("/api/v1/datasets", json={**dataset, "records": records}).json()
    ids = [
        r["id"]
        for r in client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()["items"]
    ]
    batch = make_batch(client, ids)
    store = client.app.state.services.work_store
    with client.app.state.repository.connection(write=True) as conn:
        for item, status in zip(
            batch.items[1:], ["running", "succeeded", "failed", "interrupted"], strict=True
        ):
            conn.execute(
                "UPDATE batch_items SET status=?,error_code=? WHERE id=?",
                (status, "existing", item.id),
            )
    before = store.get_batch(batch.id)
    url = f"/api/v1/evaluation-batches/{batch.id}/cancel"
    result = client.post(url)
    assert result.status_code == 200
    after = result.json()
    assert after["items"][1:] == [i.model_dump() for i in before.items[1:]]
    assert after["items"][0]["status"] == "interrupted"
    assert after["items"][0]["error_code"] == "BATCH_CANCELLED_BEFORE_START"
    assert not after["completed"]
    assert client.post(url).json() == after
    missing = client.post("/api/v1/evaluation-batches/unknown/cancel")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("first", ["claim", "cancel"])
def test_claim_and_cancel_are_serialized_by_sqlite(client, conversation, monkeypatch, first):
    batch = make_batch(client, [conversation["id"]])
    store, repo = client.app.state.services.work_store, client.app.state.repository
    store.register_owner("race", time(), 60)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    connection = repo.connection
    first_thread = None

    @contextmanager
    def held_connection(*args, **kwargs):
        with connection(*args, **kwargs) as conn:
            if kwargs.get("write") and threading.get_ident() == first_thread:
                entered.set()
                assert release.wait(5)
            yield conn

    monkeypatch.setattr(repo, "connection", held_connection)
    operations = {
        "claim": lambda: store.claim("race", time()),
        "cancel": lambda: store.cancel_pending(batch.id),
    }

    def run_first():
        nonlocal first_thread
        first_thread = threading.get_ident()
        return operations[first]()

    def run_second():
        second_started.set()
        return operations["cancel" if first == "claim" else "claim"]()

    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(run_first)
        try:
            assert entered.wait(3)
            b = pool.submit(run_second)
            assert second_started.wait(3)
        finally:
            release.set()
        first_result, second_result = a.result(5), b.result(5)
    item = store.get_batch(batch.id).items[0]
    claimed = first_result if first == "claim" else second_result
    if first == "claim":
        assert claimed.id == item.id and item.status == "running" and item.error_code is None
    else:
        assert claimed is None and item.error_code == "BATCH_CANCELLED_BEFORE_START"
    assert store.claim("race", time()) is None


def test_cancel_has_known_zero_usage_and_is_not_claimed_after_restart(
    settings, client, conversation, monkeypatch
):
    batch = make_batch(client, [conversation["id"]])
    assert client.post(f"/api/v1/evaluation-batches/{batch.id}/cancel").json()["completed"]
    usage = client.post("/api/v1/reports/usage", json={"batch_id": batch.id}).json()
    assert usage["complete"] and usage["calls_complete"] and usage["tokens_complete"]
    assert usage["known_calls"] == usage["known_duration_ms"] == usage["known_input_tokens"] == 0
    assert usage["missing_ids"] == [] and usage["pending_items"] == 0
    row = usage["rows"][0]
    assert row["known_cost"] == "0" and row["execution"]["provider_not_called"]
    assert row["limitations"] == [] and row["tariff"] is None
    assert usage["known_cost_by_currency"] == {}  # No invented tariff/currency.
    checked = threading.Event()
    claim = WorkStore.claim

    def observe_claim(self, owner, now):
        result = claim(self, owner, now)
        assert result is None
        checked.set()
        return result

    class NeverCalled:
        configured = True
        calls = 0

        async def evaluate(self, context):
            self.calls += 1
            raise AssertionError("Canceled work must not reach AI")

    model = NeverCalled()
    monkeypatch.setattr(WorkStore, "claim", observe_claim)
    with TestClient(create_app(settings, model)) as restarted:
        assert checked.wait(3)
        assert restarted.get(f"/api/v1/evaluation-batches/{batch.id}").json()["completed"]
    assert model.calls == 0


def test_cancel_does_not_interrupt_started_model_call(settings, conversation, answers):
    entered, release = threading.Event(), threading.Event()

    class BlockingModel:
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

    model = BlockingModel()
    with TestClient(create_app(settings, model)) as running:
        batch = running.post(
            "/api/v1/evaluation-batches",
            json={
                "conversation_ids": [conversation["id"]],
                "checklist_id": "freedom-broker",
                "checklist_version": 1,
            },
        ).json()
        try:
            assert entered.wait(3)
            result = running.post(f"/api/v1/evaluation-batches/{batch['id']}/cancel")
            assert result.status_code == 200 and result.json()["items"][0]["status"] == "running"
        finally:
            release.set()
        deadline = monotonic() + 3
        while monotonic() < deadline:
            result = running.get(f"/api/v1/evaluation-batches/{batch['id']}").json()
            if result["completed"]:
                break
            sleep(0.01)
        assert result["items"][0]["status"] == "succeeded" and model.calls == 1


def test_interrupted_provider_call_still_has_unknown_usage(client, conversation):
    batch = make_batch(client, [conversation["id"]])
    store = client.app.state.services.work_store
    store.register_owner("interrupted", time(), 60)
    item = store.claim("interrupted", time())
    store.reserve("interrupted", time(), "ai", None, "synthetic", conversation["id"], item.id)
    store.close_owner("interrupted", time())
    client.post(f"/api/v1/evaluation-batches/{batch.id}/cancel")
    usage = client.post("/api/v1/reports/usage", json={"batch_id": batch.id}).json()
    assert not usage["complete"] and not usage["calls_complete"]
    assert usage["rows"][0]["known_cost"] is None
    assert not usage["rows"][0]["execution"]["provider_not_called"]
    assert "USAGE_UNKNOWN" in usage["rows"][0]["limitations"]
