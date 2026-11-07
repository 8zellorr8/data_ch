import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.errors import DomainError
from backend.evaluator import EvaluatorOutput
from backend.main import create_app
from backend.models import EvaluationRecord, EvaluationResult


# Public status contract before the refactor. Expected values are independent
# of the HTTP adapter's mapping so accidental changes are caught here.
@pytest.mark.parametrize(
    "code,status",
    [
        ("NOT_FOUND", 404),
        ("STORAGE_ERROR", 500),
        ("STORAGE_BUSY", 503),
        ("VERSION_CONFLICT", 409),
        ("REVISION_CONFLICT", 409),
        ("APPEAL_EXISTS", 409),
        ("APPEAL_CLOSED", 409),
        ("UNKNOWN_CRITERION", 422),
        ("INVALID_APPEAL", 422),
        ("INVALID_RESULT", 422),
        ("INVALID_ANSWER", 422),
        ("INVALID_EVIDENCE", 422),
        ("AI_NOT_CONFIGURED", 503),
        ("AI_TIMEOUT", 504),
        ("AI_UNAVAILABLE", 502),
        ("AI_INVALID_RESULT", 502),
        ("ORGANIZER_NOT_CONFIGURED", 503),
        ("ORGANIZER_CONFIG_ERROR", 503),
        ("ORGANIZER_INVALID_RESPONSE", 502),
        ("ORGANIZER_UNAVAILABLE", 502),
        ("DATASET_TOO_LARGE", 413),
    ],
)
def test_existing_error_status_and_json_contract(client, monkeypatch, code, status):
    def failure(conversation_id):
        raise DomainError(code, "Expected failure")

    monkeypatch.setattr(client.app.state.repository, "get_conversation", failure)
    response = client.get("/api/v1/conversations/example")
    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": "Expected failure"}}


@pytest.mark.parametrize("failure", [TypeError, AttributeError, ValueError, ValidationError])
def test_internal_programming_errors_are_not_reported_as_bad_ai(
    settings, conversation, answers, monkeypatch, failure
):
    class ValidEvaluator:
        configured = True

        async def evaluate(self, context):
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="test-only",
                prompt_version="v1",
            )

    def broken_factory(*args, **kwargs):
        if failure is ValidationError:
            return EvaluationRecord.model_validate({})
        raise failure("private-debug-details")

    monkeypatch.setattr("backend.services.new_evaluation", broken_factory)
    with TestClient(create_app(settings, ValidEvaluator())) as client:
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation["id"]})
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "private-debug-details" not in response.text
        assert (
            client.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()["total"]
            == 0
        )


@pytest.mark.parametrize("field", ["author", "comment"])
def test_blank_appeal_returns_validation_error_without_writing(client, evaluation, field):
    payload = {"criterion_id": "contact", "comment": "Review", "author": "QA", field: "   "}
    response = client.post(f"/api/v1/evaluations/{evaluation['id']}/appeals", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get(f"/api/v1/evaluations/{evaluation['id']}").json()["appeals"] == []


def test_previously_saved_blank_labels_remain_readable(client, evaluation):
    # Earlier releases accepted whitespace-only labels. Tightening new requests
    # must not make existing evaluation history unreadable.
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation['id']}/appeals",
        json={
            "criterion_id": "contact",
            "author": "QA",
            "comment": "Review",
        },
    ).json()
    legacy = {k: v for k, v in evaluation.items() if k != "appeals"}
    legacy["metadata"] = {**legacy["metadata"], "author": "   "}
    appeal.update(author="   ", comment=" \t ")
    dataset_id = evaluation["conversation"]["dataset_id"]
    with client.app.state.repository.connection(write=True) as connection:
        connection.execute(
            "UPDATE evaluations SET body=? WHERE id=?", (json.dumps(legacy), evaluation["id"])
        )
        connection.execute(
            "UPDATE appeals SET body=? WHERE id=?", (json.dumps(appeal), appeal["id"])
        )
        connection.execute(
            "UPDATE datasets SET name=?, group_id=? WHERE id=?", ("   ", "   ", dataset_id)
        )
    response = client.get(f"/api/v1/datasets/{dataset_id}/export")
    assert response.status_code == 200
    exported = response.json()
    assert exported["dataset"]["name"] == "   "
    assert exported["dataset"]["group_id"] == "   "
    assert exported["evaluations"][0]["metadata"]["author"] == "   "
    assert exported["evaluations"][0]["appeals"][0]["comment"] == " \t "


def test_optional_storage_columns_do_not_break_existing_workflow(
    client, dataset, checklist, answers
):
    store = client.app.state.repository
    # Simulate a compatible schema extension. All SQL must name the fields it uses.
    with store.connection(write=True) as connection:
        for table in (
            "datasets",
            "conversations",
            "checklists",
            "checklist_versions",
            "evaluations",
            "appeals",
        ):
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN optional_note TEXT DEFAULT 'future'"
            )
    store.initialize()
    imported = client.post("/api/v1/datasets", json=dataset)
    assert imported.status_code == 201
    dataset_id = imported.json()["id"]
    assert client.get("/api/v1/datasets").json()["items"][0]["id"] == dataset_id
    conversation = client.get(f"/api/v1/datasets/{dataset_id}/conversations").json()["items"][0]
    definition = {
        k: v for k, v in checklist.items() if k not in {"checklist_id", "version", "created_at"}
    }
    custom = client.post("/api/v1/checklists", json=definition)
    assert custom.status_code == 201
    checklist_id = custom.json()["checklist_id"]
    new_version = client.post(
        f"/api/v1/checklists/{checklist_id}/versions",
        json={"expected_version": 1, "definition": definition},
    )
    assert new_version.status_code == 201
    assert client.get(f"/api/v1/checklists/{checklist_id}").json()["version"] == 2
    assert client.get("/api/v1/checklists").status_code == 200
    evaluation = client.post(
        "/api/v1/evaluations/manual",
        json={
            "conversation_id": conversation["id"],
            "checklist_id": checklist_id,
            "checklist_version": 2,
            "author": "QA",
            "result": answers,
        },
    )
    assert evaluation.status_code == 201
    evaluation_id = evaluation.json()["id"]
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation_id}/appeals",
        json={"criterion_id": "contact", "author": "QA", "comment": "Review"},
    )
    assert appeal.status_code == 201
    resolved = client.post(
        f"/api/v1/appeals/{appeal.json()['id']}/decision",
        json={
            "expected_revision": 1,
            "value": "no",
            "reason": "Correction",
            "reviewer": "QA",
            "evidence_kind": "missing",
            "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
        },
    )
    assert resolved.status_code == 200 and resolved.json()["score"]["total"] == 90
    exported = client.get(f"/api/v1/datasets/{dataset_id}/export")
    assert exported.status_code == 200
    assert exported.json()["evaluations"][0]["revision"] == 2
    assert "optional_note" not in exported.json()["dataset"]
