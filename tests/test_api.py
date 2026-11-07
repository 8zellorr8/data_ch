from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.evaluator import EvaluatorOutput
from backend.main import create_app
from backend.models import EvaluationResult


def test_health_default_rubric_and_openapi(client, checklist):
    assert client.get("/health").json()["ai_configured"] is False
    assert len(checklist["criteria"]) == 8
    assert sum(c["weight"] for c in checklist["criteria"]) == 100
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/api/v1/checklists/freedom-broker/versions/0").status_code == 422


def test_import_preserves_voice_unicode_and_timestamps(client, dataset):
    kz = deepcopy(dataset["records"][0])
    kz.update(
        conversation_id="kz-call",
        language="KZ",
        channel="voice call",
        audio_url="https://example.com/synthetic.wav",
    )
    kz["transcript"][0].update(text="Сәлеметсіз бе!", timestamp="2026-10-01T14:03:11+05:00")
    en = deepcopy(dataset["records"][0])
    en.update(conversation_id="en-chat", language="EN")
    dataset["records"].extend([kz, en])
    imported = client.post("/api/v1/datasets", json=dataset).json()
    assert imported["count"] == 3
    page = client.get(
        f"/api/v1/datasets/{imported['id']}/conversations",
        params={"language": "KZ", "channel": "voice call", "limit": 1},
    ).json()
    assert page["total"] == 1
    record = client.get(f"/api/v1/conversations/{page['items'][0]['id']}").json()
    assert record["conversation_id"] == "kz-call"
    assert record["transcript"][0] == {**kz["transcript"][0], "id": "m1"}
    assert record["audio_url"] == kz["audio_url"]
    assert (
        client.get(f"/api/v1/datasets/{imported['id']}/conversations?offset=2&limit=1").json()[
            "total"
        ]
        == 3
    )


def test_invalid_import_is_atomic_and_ids_are_per_dataset(client, dataset):
    bad = deepcopy(dataset)
    bad["records"].append(deepcopy(bad["records"][0]))
    assert client.post("/api/v1/datasets", json=bad).status_code == 422
    assert client.get("/api/v1/datasets").json()["items"] == []
    first = client.post("/api/v1/datasets", json=dataset).json()
    second = client.post("/api/v1/datasets", json=dataset).json()
    a = client.get(f"/api/v1/datasets/{first['id']}/conversations").json()["items"][0]
    b = client.get(f"/api/v1/datasets/{second['id']}/conversations").json()["items"][0]
    assert a["id"] != b["id"] and a["conversation_id"] == b["conversation_id"]


def test_no_model_never_saves_fabricated_result(client, conversation):
    response = client.post("/api/v1/evaluations", json={"conversation_id": conversation["id"]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    assert (
        client.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()["total"] == 0
    )
    assert (
        client.post("/api/v1/datasets/import-organizer", json={}).json()["error"]["code"]
        == "ORGANIZER_NOT_CONFIGURED"
    )


def test_custom_checklist_versions_do_not_mutate_evaluations(client, checklist, evaluation):
    definition = {
        k: v for k, v in checklist.items() if k not in ("checklist_id", "version", "created_at")
    }
    definition["criteria"][0]["weight"] = 35
    payload = {"expected_version": 1, "definition": definition}
    response = client.post("/api/v1/checklists/freedom-broker/versions", json=payload)
    assert response.status_code == 201 and response.json()["version"] == 2
    assert (
        client.post("/api/v1/checklists/freedom-broker/versions", json=payload).status_code == 409
    )
    assert client.get("/api/v1/checklists/freedom-broker").json()["version"] == 2
    assert (
        client.get("/api/v1/checklists/freedom-broker/versions/1").json()["criteria"][0]["weight"]
        == 10
    )
    saved = client.get(f"/api/v1/evaluations/{evaluation['id']}").json()
    assert saved["checklist"]["version"] == 1
    assert saved["input_hash"] == evaluation["input_hash"]
    custom = client.post(
        "/api/v1/checklists", json={"name": "New rubric", "criteria": [definition["criteria"][0]]}
    )
    assert custom.status_code == 201 and custom.json()["checklist_id"] != "freedom-broker"


@pytest.mark.parametrize(
    "change",
    [
        "bad_quote",
        "bad_message",
        "no_evidence",
        "blank_explanation",
        "unjustified_na",
        "no_limitation",
    ],
)
def test_bad_evidence_and_unjustified_answers_not_saved(client, conversation, answers, change):
    answer = answers["answers"][0]
    if change == "bad_quote":
        answer["evidence"][0]["quote"] = "This never occurred"
    elif change == "bad_message":
        answer["evidence"][0]["message_id"] = "m999"
    elif change == "no_evidence":
        answer["evidence"] = []
    elif change == "blank_explanation":
        answer["explanation"] = "   "
    elif change == "unjustified_na":
        answers["answers"][1]["value"] = "na"
    else:
        answer["needs_review"] = True
    response = client.post(
        "/api/v1/evaluations/manual",
        json={"conversation_id": conversation["id"], "author": "reviewer", "result": answers},
    )
    assert response.status_code == 422
    assert (
        client.get(f"/api/v1/conversations/{conversation['id']}/evaluations").json()["total"] == 0
    )


def test_appeal_is_audited_and_conflicting_decision_cannot_overwrite(client, evaluation):
    url = f"/api/v1/evaluations/{evaluation['id']}/appeals"
    body = {"criterion_id": "contact", "comment": "Recheck", "author": "operator"}
    appeal = client.post(url, json=body).json()
    assert client.post(url, json=body).status_code == 409
    another = client.post(url, json={**body, "criterion_id": "needs"}).json()
    decision = {
        "expected_revision": 1,
        "value": "no",
        "reason": "Reviewer correction",
        "reviewer": "QA",
        "evidence_kind": "missing",
        "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
    }
    response = client.post(f"/api/v1/appeals/{appeal['id']}/decision", json=decision)
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["revision"] == 2 and updated["score"]["total"] == 90
    assert updated["original"] == evaluation["original"]
    assert updated["original_score"]["total"] == 100
    assert updated["reviews"][0]["reviewer"] == "QA"
    assert client.post(f"/api/v1/appeals/{appeal['id']}/decision", json=decision).status_code == 409
    conflict = client.post(f"/api/v1/appeals/{another['id']}/decision", json=decision)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "REVISION_CONFLICT"
    decision.update(expected_revision=2, value="na", evidence_kind="not_applicable")
    final = client.post(f"/api/v1/appeals/{another['id']}/decision", json=decision).json()
    assert final["revision"] == 3 and final["score"]["total"] == 88.24
    assert final["original"] == evaluation["original"]


def test_invalid_appeal_rolls_back(client, evaluation):
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation['id']}/appeals",
        json={"criterion_id": "contact", "author": "test", "comment": "Test"},
    ).json()
    response = client.post(
        f"/api/v1/appeals/{appeal['id']}/decision",
        json={
            "expected_revision": 1,
            "value": "no",
            "reason": "Correction",
            "reviewer": "QA",
            "evidence_kind": "missing",
            "evidence": [{"message_id": "m1", "quote": "Invented quote"}],
        },
    )
    assert response.status_code == 422
    current = client.get(f"/api/v1/evaluations/{evaluation['id']}").json()
    assert current["revision"] == 1 and current["appeals"][0]["status"] == "open"


def test_export_and_restart_keep_history(client, settings, evaluation):
    exported = client.get(
        f"/api/v1/datasets/{evaluation['conversation']['dataset_id']}/export"
    ).json()
    assert exported["schema_version"] == 1
    assert exported["evaluations"][0]["origin"] == "manual"
    assert exported["conversations"][0]["transcript"][0]["id"] == "m1"
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/api/v1/evaluations/{evaluation['id']}").json() == evaluation


def test_injected_evaluator_receives_new_input_each_time(settings, dataset, answers):
    class TestEvaluator:
        configured = True
        seen = []

        async def evaluate(self, context):
            self.seen.append(context.conversation.id)
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(answers),
                model="test-only",
                prompt_version="test-v1",
            )

    evaluator = TestEvaluator()
    with TestClient(create_app(settings, evaluator)) as client:
        ids = []
        for _ in range(2):
            imported = client.post("/api/v1/datasets", json=dataset).json()
            conversation_id = client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()[
                "items"
            ][0]["id"]
            ids.append(conversation_id)
            response = client.post("/api/v1/evaluations", json={"conversation_id": conversation_id})
            assert response.status_code == 201, response.text
            assert response.json()["origin"] == "ai"
            assert response.json()["metadata"]["model"] == "test-only"
        assert evaluator.seen == ids and ids[0] != ids[1]


@pytest.mark.parametrize("mode", ["exception", "invalid_object", "bad_quote"])
def test_ai_adapter_failures_save_nothing(settings, dataset, answers, mode):
    class BrokenEvaluator:
        configured = True

        async def evaluate(self, context):
            if mode == "exception":
                raise RuntimeError("Provider secret must not be returned")
            if mode == "invalid_object":
                return None
            bad = deepcopy(answers)
            bad["answers"][0]["evidence"][0]["quote"] = "Invented"
            return EvaluatorOutput(
                result=EvaluationResult.model_validate(bad), model="test", prompt_version="v1"
            )

    with TestClient(create_app(settings, BrokenEvaluator())) as client:
        imported = client.post("/api/v1/datasets", json=dataset).json()
        conversation_id = client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()[
            "items"
        ][0]["id"]
        response = client.post("/api/v1/evaluations", json={"conversation_id": conversation_id})
        assert response.status_code == 502
        assert "secret" not in response.text
        assert (
            client.get(f"/api/v1/conversations/{conversation_id}/evaluations").json()["total"] == 0
        )


def test_limits_cors_and_error_contract(client):
    assert client.get("/api/v1/datasets/missing/conversations").status_code == 404
    assert (
        client.post(
            "/api/v1/datasets", content="{", headers={"Content-Type": "application/json"}
        ).json()["error"]["code"]
        == "VALIDATION_ERROR"
    )
    assert client.post("/api/v1/datasets", content=b"x" * (8 * 1024 * 1024 + 1)).status_code == 413
    response = client.options(
        "/api/v1/datasets",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    denied = client.options(
        "/api/v1/datasets",
        headers={"Origin": "https://unknown.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "flag", "weight": 10},
        {"weight": -1},
        {"allow_na": True},
        {"type": "scale", "scale": [0, 0, 10]},
        {"type": "scale", "scale": [-1, 10]},
    ],
)
def test_invalid_rubrics_rejected(client, changes):
    criterion = {"id": "x", "name": "X", "type": "binary", "weight": 10, "rule": "Rule", **changes}
    response = client.post("/api/v1/checklists", json={"name": "Invalid", "criteria": [criterion]})
    assert response.status_code == 422
