"""Integration regressions for concurrency, provider isolation and HTTP contracts."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.errors import DomainError
from backend.evaluator import EvaluatorOutput
from backend.main import create_app
from backend.models import AppealDecision, ChecklistDefinition, EvaluationRequest, EvaluationResult
from backend.services import EvaluationService


def decision(revision=1):
    return AppealDecision(
        expected_revision=revision,
        value="no",
        reason="Review correction",
        reviewer="QA",
        evidence_kind="missing",
        evidence=[{"message_id": "m1", "quote": "Где инструкция?"}],
    )


def open_appeal(client, evaluation, criterion="contact"):
    response = client.post(
        f"/api/v1/evaluations/{evaluation['id']}/appeals",
        json={
            "criterion_id": criterion,
            "comment": "Recheck",
            "author": "Operator",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_simultaneous_versions_have_one_winner(client, checklist):
    store = client.app.state.repository
    definition = ChecklistDefinition.model_validate(
        {k: v for k, v in checklist.items() if k not in {"checklist_id", "version", "created_at"}}
    )
    barrier = threading.Barrier(2)

    def update():
        barrier.wait(timeout=5)
        try:
            return store.new_version("freedom-broker", 1, definition).version
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: update(), range(2)))
    assert set(results) == {2, "VERSION_CONFLICT"}
    assert store.get_checklist("freedom-broker").version == 2


def test_two_reviewers_cannot_overwrite_each_other(client, evaluation, monkeypatch):
    first = open_appeal(client, evaluation, "contact")
    second = open_appeal(client, evaluation, "needs")
    store = client.app.state.repository
    service = client.app.state.services.appeals
    original_commit = store.commit_review
    barrier = threading.Barrier(2)

    def simultaneous_commit(*args):
        barrier.wait(timeout=5)
        return original_commit(*args)

    monkeypatch.setattr(store, "commit_review", simultaneous_commit)

    def review(appeal_id):
        try:
            return service.decide(appeal_id, decision()).revision
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(review, [first["id"], second["id"]]))
    assert set(results) == {2, "REVISION_CONFLICT"}
    final = store.get_evaluation(evaluation["id"])
    assert final.revision == 2 and len(final.reviews) == 1
    assert sorted(a.status for a in final.appeals) == ["open", "resolved"]
    assert final.original.model_dump() == evaluation["original"]


def test_review_storage_failure_rolls_back_both_records(client, evaluation):
    appeal = open_appeal(client, evaluation)
    store = client.app.state.repository
    with store.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER fail_review BEFORE UPDATE ON appeals BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    response = client.post(f"/api/v1/appeals/{appeal['id']}/decision", json=decision().model_dump())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "STORAGE_ERROR"
    assert "test failure" not in response.text
    current = store.get_evaluation(evaluation["id"])
    assert current.revision == 1 and current.score.total == 100
    assert current.appeals[0].status == "open" and current.reviews == []


def test_read_transaction_keeps_a_consistent_snapshot(client, evaluation):
    appeal = open_appeal(client, evaluation)
    store = client.app.state.repository
    with store.connection() as connection:
        before = store._get_evaluation(connection, evaluation["id"])
        client.app.state.services.appeals.decide(appeal["id"], decision())
        snapshot = store._get_evaluation(connection, evaluation["id"])
    assert snapshot == before
    assert snapshot.revision == 1 and snapshot.appeals[0].status == "open"
    assert store.get_evaluation(evaluation["id"]).revision == 2


def test_locked_database_returns_explicit_retryable_error(client, dataset):
    store = client.app.state.repository
    store.timeout = 0.01
    with store.connection(write=True):
        response = client.post("/api/v1/datasets", json=dataset)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_BUSY"
    assert store.list_datasets() == []


@pytest.mark.parametrize(
    "mutation", ["rubric", "forged_quote", "invalid_answer", "invalid_metadata"]
)
def test_provider_cannot_change_original_inputs_or_bypass_validation(
    settings, conversation, answers, mutation
):
    class MutableEvaluator:
        configured = True

        async def evaluate(self, context):
            result = EvaluationResult.model_validate(deepcopy(answers))
            if mutation == "rubric":
                context.checklist.criteria[0].weight = 999
            elif mutation == "forged_quote":
                context.conversation.transcript[1].text = "Forged text"
                result.answers[0].evidence[0].quote = "Forged text"
            elif mutation == "invalid_answer":
                result.answers[0].value = True
            return EvaluatorOutput(
                result=result,
                model="test-only",
                prompt_version="v1",
                calls=-1 if mutation == "invalid_metadata" else 1,
            )

    with TestClient(create_app(settings, MutableEvaluator())) as client:
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation["id"]})
        if mutation == "rubric":
            assert response.status_code == 201
            assert response.json()["checklist"]["criteria"][0]["weight"] == 10
            assert response.json()["conversation"] == conversation
        else:
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "AI_INVALID_RESULT"
            assert (
                client.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()[
                    "total"
                ]
                == 0
            )


def test_model_timeout_cancels_request_without_saving(settings, conversation):
    cancelled = threading.Event()

    class SlowEvaluator:
        configured = True

        async def evaluate(self, context):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

    with TestClient(
        create_app(replace(settings, evaluation_timeout=0.02), SlowEvaluator())
    ) as client:
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation["id"]})
        assert response.status_code == 504 and response.json()["error"]["code"] == "AI_TIMEOUT"
        assert cancelled.is_set()
        assert (
            client.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()["total"]
            == 0
        )


def test_model_success_does_not_disguise_storage_failure(
    settings, conversation, answers, monkeypatch
):
    class ValidEvaluator:
        configured = True

        async def evaluate(self, context):
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="test-only",
                prompt_version="v1",
            )

    app = create_app(settings, ValidEvaluator())

    def unavailable(record):
        raise DomainError("STORAGE_BUSY", "Storage is busy")

    monkeypatch.setattr(app.state.repository, "insert_evaluation", unavailable)
    with TestClient(app) as client:
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation["id"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_BUSY"


def test_storage_wait_does_not_block_event_loop(client, conversation, answers, monkeypatch):
    store = client.app.state.repository
    original_get = store.get_conversation
    entered, release = threading.Event(), threading.Event()

    def blocked_read(conversation_id):
        entered.set()
        assert release.wait(timeout=3), "Event loop could not release blocked storage"
        return original_get(conversation_id)

    monkeypatch.setattr(store, "get_conversation", blocked_read)

    class ValidEvaluator:
        configured = True

        async def evaluate(self, context):
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="test-only",
                prompt_version="v1",
            )

    async def scenario():
        service = EvaluationService(store, ValidEvaluator())
        task = asyncio.create_task(
            service.evaluate(EvaluationRequest(conversation_id=conversation["id"]))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # This coroutine must progress while the synchronous read is blocked.
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        return await task

    assert asyncio.run(scenario()).origin == "ai"


def test_organizer_storage_also_runs_outside_event_loop(settings, dataset):
    class Source:
        loop_thread = None

        async def fetch(self, request):
            from backend.models import DatasetImport

            self.loop_thread = threading.get_ident()
            return DatasetImport.model_validate(dataset)

    source = Source()
    app = create_app(settings, dataset_source=source)
    original = app.state.repository.import_dataset

    def checked_import(payload):
        assert threading.get_ident() != source.loop_thread
        return original(payload)

    app.state.repository.import_dataset = checked_import
    with TestClient(app) as client:
        assert client.post("/api/v1/datasets/import-organizer", json={}).status_code == 201


def test_openapi_describes_all_success_and_error_responses(client):
    schema = client.get("/openapi.json").json()
    for path in schema["paths"].values():
        for operation in path.values():
            responses = operation["responses"]
            success = responses.get("200") or responses["201"]
            assert success["content"]["application/json"]["schema"].get("$ref")
            assert responses["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
                "/ErrorResponse"
            )


def test_large_body_error_has_cors_headers(client):
    response = client.post(
        "/api/v1/datasets",
        content=b"x" * (8 * 1024 * 1024 + 1),
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_unexpected_errors_are_redacted_and_readable_by_frontend(client, monkeypatch, caplog):
    def broken():
        raise RuntimeError("secret-password-and-client-transcript")

    monkeypatch.setattr(client.app.state.repository, "list_datasets", broken)
    response = client.get("/api/v1/datasets", headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "secret-password" not in response.text + caplog.text


def test_chunked_body_limit_before_application_is_called():
    from backend.http import BodyLimitMiddleware

    calls = []

    async def app(scope, receive, send):
        calls.append(await receive())

    async def run(chunks, limit):
        messages = iter(
            [
                {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
                for index, chunk in enumerate(chunks)
            ]
        )
        sent = []

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await BodyLimitMiddleware(app, limit)({"type": "http", "method": "POST"}, receive, send)
        return sent

    response = asyncio.run(run([b"123", b"456"], 5))
    assert not calls and response[0]["status"] == 413
    asyncio.run(run([b"12", b"345"], 5))
    assert calls == [{"type": "http.request", "body": b"12345", "more_body": False}]
