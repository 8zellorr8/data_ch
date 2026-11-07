import json
import subprocess
import sys
from copy import deepcopy
from uuid import uuid4

import pytest

from backend.db import Repository
from backend.domain import new_evaluation
from backend.models import AIModelMetadata, ChecklistDefinition, EvaluationResult
from backend.reports import ComparisonManifest, compare_evaluations


def add_ai(repo, reference, result=None):
    ai = new_evaluation(
        reference.conversation,
        reference.checklist,
        result or deepcopy(reference.original),
        AIModelMetadata(model="synthetic-test-only", prompt_version="v1", duration_ms=1),
        record_id=str(uuid4()),
        created_at=reference.created_at,
    )
    return repo.insert_evaluation(ai)


def pair(reference, ai):
    return {"reference_id": reference.id, "reference_revision": reference.revision, "ai_id": ai.id}


def test_quality_api_matches_cli_and_has_no_transcript(client, evaluation, settings, tmp_path):
    repo = client.app.state.repository
    reference = repo.get_evaluation(evaluation["id"])
    ai = add_ai(repo, reference)
    selection = {"pairs": [pair(reference, ai)]}
    manifest, output = tmp_path / "selection.json", tmp_path / "report.json"
    manifest.write_text(json.dumps(selection), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.benchmark",
            "--database",
            str(settings.database_path),
            "--manifest",
            str(manifest),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    response = client.post("/api/v1/reports/quality", json=selection)
    assert response.status_code == 200
    assert response.json() == json.loads(output.read_text(encoding="utf-8"))
    assert response.json()["mae"] == "0.0" and response.json()["coverage"] == 1
    assert "transcript" not in response.text and "Здравствуйте" not in response.text
    assert "not the organizer ranking formula" in response.json()["purpose"]
    with repo.connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM evaluation_executions WHERE scope='ai'").fetchone()[
                0
            ]
            == 0
        )


def test_quality_api_reports_missing_changed_revision_and_input_mismatch(client, evaluation):
    repo = client.app.state.repository
    reference = repo.get_evaluation(evaluation["id"])
    ai = add_ai(repo, reference)
    selected = pair(reference, ai)
    cases = [
        ({**selected, "reference_id": "missing"}, "MISSING_REFERENCE"),
        ({**selected, "ai_id": None}, "MISSING_AI"),
        ({**selected, "ai_id": "missing"}, "MISSING_AI"),
        ({**selected, "reference_revision": 2}, "REFERENCE_REVISION_CHANGED"),
        ({**selected, "ai_id": reference.id}, "WRONG_ORIGIN"),
    ]
    for value, reason in cases:
        data = client.post("/api/v1/reports/quality", json={"pairs": [value]}).json()
        assert data["pairs"][0]["issue"] == reason
        assert data["compared"] == 0 and data["mae"] is None and data["coverage"] == 0
    for change in [
        {"input_hash": "different"},
        {"conversation_id": "different"},
        {"checklist": ai.checklist.model_copy(update={"version": 2})},
    ]:
        mismatched = ai.model_copy(update=change)
        with repo.connection(write=True) as conn:
            conn.execute(
                "UPDATE evaluations SET body=? WHERE id=?", (mismatched.model_dump_json(), ai.id)
            )
        data = client.post("/api/v1/reports/quality", json={"pairs": [selected]}).json()
        assert data["pairs"][0]["issue"] == "INPUT_MISMATCH" and data["score_compared"] == 0


def test_quality_rejects_stale_reference_after_real_review_and_keeps_original_ai(
    client, evaluation
):
    repo = client.app.state.repository
    reference = repo.get_evaluation(evaluation["id"])
    ai = add_ai(repo, reference)
    selection = {"pairs": [pair(reference, ai)]}
    for record_id in [ai.id, reference.id]:
        appeal = client.post(
            f"/api/v1/evaluations/{record_id}/appeals",
            json={
                "criterion_id": "contact",
                "author": "QA",
                "comment": "Synthetic review",
            },
        ).json()
        decision = client.post(
            f"/api/v1/appeals/{appeal['id']}/decision",
            json={
                "expected_revision": 1,
                "value": "no",
                "reason": "Synthetic correction",
                "reviewer": "QA",
                "evidence_kind": "missing",
                "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
            },
        )
        assert decision.status_code == 200
        report = client.post("/api/v1/reports/quality", json=selection).json()
        if record_id == ai.id:
            assert report["compared"] == 1 and float(report["mae"]) == 0
        else:
            assert report["pairs"][0]["issue"] == "REFERENCE_REVISION_CHANGED"
    selection["pairs"][0]["reference_revision"] = 2
    report = client.post("/api/v1/reports/quality", json=selection).json()
    assert report["compared"] == 1 and float(report["mae"]) > 0


def test_quality_reads_all_selections_in_one_wal_snapshot(client, evaluation, monkeypatch):
    repo = client.app.state.repository
    reference = repo.get_evaluation(evaluation["id"])
    ai = add_ai(repo, reference)
    original_read, changed = Repository._get_evaluation, False

    def read_and_write(conn, evaluation_id):
        nonlocal changed
        record = original_read(conn, evaluation_id)
        if not changed:
            changed = True
            # A second connection commits between the two reads of a report.
            with repo.connection(write=True) as writer:
                for item in (reference, ai):
                    updated = item.model_copy(update={"revision": 2})
                    writer.execute(
                        "UPDATE evaluations SET revision=2,body=? WHERE id=?",
                        (updated.model_dump_json(), item.id),
                    )
        return record

    monkeypatch.setattr(Repository, "_get_evaluation", staticmethod(read_and_write))
    response = client.post("/api/v1/reports/quality", json={"pairs": [pair(reference, ai)]})
    assert response.status_code == 200 and response.json()["compared"] == 1
    assert repo.get_evaluation(reference.id).revision == 2
    assert (
        client.post("/api/v1/reports/quality", json={"pairs": [pair(reference, ai)]}).json()[
            "compared"
        ]
        == 0
    )


def test_quality_preserves_na_flags_and_null_denominators(client, conversation, answers):
    repo = client.app.state.repository
    default = repo.get_checklist("freedom-broker", 1)
    criteria = [
        c.model_copy(update={"allow_na": True, "na_rule": "Synthetic not applicable"})
        for c in default.criteria
    ]
    rubric = repo.create_checklist(ChecklistDefinition(name="Synthetic all NA", criteria=criteria))
    na_result = EvaluationResult.model_validate(answers)
    for answer in na_result.answers:
        answer.value, answer.evidence_kind, answer.na_reason = "na", "not_applicable", "Synthetic"
    result = client.post(
        "/api/v1/evaluations/manual",
        json={
            "conversation_id": conversation["id"],
            "checklist_id": rubric.checklist_id,
            "checklist_version": rubric.version,
            "author": "QA",
            "result": na_result.model_dump(),
        },
    )
    assert result.status_code == 201, result.text
    reference = repo.get_evaluation(result.json()["id"])
    ai = add_ai(repo, reference, EvaluationResult.model_validate(answers))
    selection = ComparisonManifest.model_validate({"pairs": [pair(reference, ai)]})
    data = client.post("/api/v1/reports/quality", json=selection.model_dump()).json()
    expected = compare_evaluations(selection, {reference.id: reference, ai.id: ai})
    assert data == expected.model_dump(mode="json")
    assert data["score_null_excluded"] == 1 and data["score_compared"] == 0 and data["mae"] is None
    assert all(m["na_false_negative"] == 1 for m in data["criteria"])
    assert all(m["flag_na_excluded"] == 1 for m in data["criteria"] if m["kind"] == "flag")


@pytest.mark.parametrize(
    "payload",
    [
        {"pairs": []},
        {"pairs": [{"reference_id": "x", "ai_id": "y"}]},
        {"pairs": [{"reference_id": "x", "reference_revision": 1}] * 2},
        {"pairs": [{"reference_id": str(i), "reference_revision": 1} for i in range(1001)]},
    ],
)
def test_quality_request_limits_and_explicit_selection(client, payload):
    assert client.post("/api/v1/reports/quality", json=payload).status_code == 422
