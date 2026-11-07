"""Synthetic organizer-format checks; these are NOT model accuracy measurements."""

import json
import subprocess
import sys
from copy import deepcopy
from uuid import uuid4

import pytest

from backend.domain import new_evaluation
from backend.models import AIModelMetadata, DatasetImport, EvaluationResult, ManualMetadata
from backend.submission import SubmissionRequest
from backend.submission_remote import download, endpoint
from backend.work_models import BatchRequest


@pytest.fixture
def submission_data(client, dataset, answers):
    repo = client.app.state.repository
    records = []
    for index in range(10):
        raw = deepcopy(dataset["records"][0])
        raw["conversation_id"] = f"original-{index + 1}"
        raw["transcript"].insert(0, {"speaker": "system", "timestamp": "08:00", "text": "Opened"})
        raw["transcript"].insert(1, {"speaker": "bot", "timestamp": "08:01", "text": "Welcome"})
        records.append(raw)
    dataset_id = repo.import_dataset(DatasetImport(name="Synthetic PROD", records=records)).id
    rubric = repo.get_checklist("freedom-broker", 1)
    result = EvaluationResult.model_validate(answers)
    for answer in result.answers:
        answer.evidence[0].message_id = "m4"
        if answer.criterion_id in ("needs", "identity"):
            answer.value = "na"
            answer.evidence_kind = "not_applicable"
            answer.na_reason = "Synthetic N/A"
        elif answer.criterion_id == "consultation":
            answer.value = 15
        elif answer.criterion_id == "management":
            answer.value = 7
    selected = []
    for conversation in repo.export_dataset(dataset_id).conversations[:9]:
        record = repo.insert_evaluation(
            new_evaluation(
                conversation,
                rubric,
                result,
                AIModelMetadata(model="synthetic-test-only", prompt_version="test", duration_ms=1),
                record_id=str(uuid4()),
                created_at="2026-01-01T00:00:00Z",
            )
        )
        selected.append(
            {"conversation_id": conversation.conversation_id, "evaluation_id": record.id}
        )
    selected.append({"conversation_id": "original-10", "error": "AI_TIMEOUT"})
    return dataset_id, {
        "split": "prod",
        "team_name": "Тестовая команда",
        "model_mode": "external_api",
        "model_names": ["synthetic-test-only"],
        "items": selected,
    }


def submit(client, prepared):
    dataset_id, payload = prepared
    return client.post(f"/api/v1/datasets/{dataset_id}/submission", json=payload)


def run_cli(settings, dataset_id, manifest, output, *flags):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "backend.submission_cli",
            "--database",
            str(settings.database_path),
            "--dataset-id",
            dataset_id,
            "--manifest",
            str(manifest),
            "--output",
            str(output),
            *flags,
        ],
        capture_output=True,
        timeout=15,
    )


def test_api_and_cli_match_appendix_and_keep_old_export(
    client, submission_data, settings, tmp_path
):
    dataset_id, payload = submission_data
    response = submit(client, submission_data)
    assert response.status_code == 200, response.text
    doc = response.json()
    remote = download(
        client,
        endpoint("http://localhost", dataset_id),
        SubmissionRequest.model_validate(payload),
        timeout=5,
    )
    assert remote.model_dump(mode="json") == doc
    assert doc["schema_version"] == "1.0"
    assert len(doc["results"]) == 10
    row = doc["results"][0]
    assert row["conversation_id"] == "original-1"
    assert row["score"] == {"final_score": 85.38, "critical_flag": False}
    assert [c["criterion_id"] for c in row["criteria"]] == [
        "contact",
        "discovery",
        "identification",
        "consultation",
        "empathy",
        "dialogue",
        "closure",
        "critical",
    ]
    assert [c["value"] for c in row["criteria"]] == [True, None, None, 15, True, 7, True, False]
    assert all(c["evidence_turns"] == [4] for c in row["criteria"])
    assert doc["results"][-1] == {
        "conversation_id": "original-10",
        "status": "error",
        "criteria": [],
        "score": None,
        "error": "AI_TIMEOUT",
    }
    assert "transcript" not in response.text and "audio_url" not in response.text
    assert client.get("/health").json()["ai_configured"] is False
    old = client.get(f"/api/v1/datasets/{dataset_id}/export").json()
    assert old["schema_version"] == 1 and len(old["conversations"]) == 10
    assert old["evaluations"][0]["original"]["answers"][1]["criterion_id"] == "needs"
    manifest, output = tmp_path / "manifest.json", tmp_path / "results.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = run_cli(settings, dataset_id, manifest, output)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == doc
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert run_cli(settings, dataset_id, manifest, output, "--check").returncode == 0
    assert run_cli(settings, dataset_id, manifest, output).returncode == 2
    assert run_cli(settings, dataset_id, manifest, output, "--overwrite").returncode == 0
    with client.app.state.repository.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_executions").fetchone()[0] == 0


def test_submission_preserves_original_after_appeal(client, submission_data):
    _, payload = submission_data
    evaluation_id = payload["items"][0]["evaluation_id"]
    before = submit(client, submission_data).json()
    appeal = client.post(
        f"/api/v1/evaluations/{evaluation_id}/appeals",
        json={
            "criterion_id": "gross",
            "author": "QA",
            "comment": "Synthetic review",
        },
    ).json()
    response = client.post(
        f"/api/v1/appeals/{appeal['id']}/decision",
        json={
            "expected_revision": 1,
            "value": "yes",
            "reviewer": "QA",
            "reason": "Synthetic",
            "evidence_kind": "present",
            "evidence": [{"message_id": "m4", "quote": "Здравствуйте"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["score"]["flags"] == ["gross"]
    assert submit(client, submission_data).json() == before


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate", "foreign", "unknown_evaluation", "model"]
)
def test_invalid_selection_is_explicit_error(client, submission_data, problem):
    _, payload = submission_data
    if problem == "missing":
        payload["items"].pop()
    elif problem == "duplicate":
        payload["items"][-1] = deepcopy(payload["items"][0])
    elif problem == "foreign":
        payload["items"][0]["evaluation_id"] = payload["items"][1]["evaluation_id"]
    elif problem == "unknown_evaluation":
        payload["items"][0]["evaluation_id"] = "missing"
    else:
        payload["model_names"] = ["invented-model"]
    assert submit(client, submission_data).status_code == 422


@pytest.mark.parametrize("problem", ["manual", "weight", "na", "scale", "quote", "snapshot"])
def test_rejects_incompatible_or_corrupt_evaluation(client, submission_data, problem):
    repo = client.app.state.repository
    record = repo.get_evaluation(submission_data[1]["items"][0]["evaluation_id"])
    if problem == "manual":
        record.origin, record.metadata = "manual", ManualMetadata(author="QA")
    elif problem == "weight":
        record.checklist.criteria[0].weight = 11
    elif problem == "na":
        record.checklist.criteria[0].allow_na = True
        record.checklist.criteria[0].na_rule = "Changed"
    elif problem == "scale":
        record.original.answers[3].value = 13
    elif problem == "quote":
        record.original.answers[0].evidence[0].quote = "Not in transcript"
    else:
        record.conversation.transcript[0].text = "Changed snapshot"
    with repo.connection(write=True) as conn:
        conn.execute(
            "UPDATE evaluations SET body=? WHERE id=?", (record.model_dump_json(), record.id)
        )
    assert submit(client, submission_data).status_code == 422


def test_flags_do_not_zero_score_and_turn_ids_are_not_parsed(client, submission_data):
    from backend.submission import export_assessment

    record = client.app.state.repository.get_evaluation(
        submission_data[1]["items"][0]["evaluation_id"]
    )
    record.original.answers[-1].value = "yes"
    record.conversation.transcript[-1].id = "operator-last"
    for answer in record.original.answers:
        answer.evidence[0].message_id = "operator-last"
    output = export_assessment(record, record.conversation)
    assert output.score.final_score == 85.38 and output.score.critical_flag is True
    assert all(c.evidence_turns == [4] for c in output.criteria)


def test_batch_export_includes_errors_and_rejects_unfinished(client, submission_data):
    repo, work = client.app.state.repository, client.app.state.services.work_store
    dataset_id, payload = submission_data
    conversations = repo.export_dataset(dataset_id).conversations
    batch = work.create_batch(
        BatchRequest(
            conversation_ids=[c.id for c in conversations],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    payload["batch_id"] = batch.id
    outcomes = payload.pop("items")
    response = submit(client, submission_data)
    assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_SUBMISSION"
    # Persist synthetic terminal outcomes; no model/worker is invoked by this test.
    with repo.connection(write=True) as conn:
        for item, selected in zip(batch.items, outcomes, strict=True):
            conn.execute(
                "UPDATE batch_items SET status=?,evaluation_id=?,error_code=? WHERE id=?",
                (
                    "succeeded" if "evaluation_id" in selected else "failed",
                    selected.get("evaluation_id"),
                    selected.get("error"),
                    item.id,
                ),
            )
    response = submit(client, submission_data)
    assert response.status_code == 200, response.text
    assert response.json()["results"][-1]["error"] == "AI_TIMEOUT"
    payload["batch_id"] = "missing"
    assert submit(client, submission_data).status_code == 404


def test_dev_counts_and_prod_channels(client, dataset):
    repo = client.app.state.repository
    records = [
        {
            **deepcopy(dataset["records"][0]),
            "conversation_id": f"id-{i}",
            "channel": "voice call" if i >= 60 else "agent chat",
        }
        for i in range(65)
    ]
    dataset_id = repo.import_dataset(DatasetImport(name="Synthetic DEV", records=records)).id
    payload = {
        "split": "dev",
        "team_name": "Test",
        "model_mode": "local",
        "model_names": ["synthetic-failed-model"],
        "items": [
            {"conversation_id": c["conversation_id"], "error": "AI_UNAVAILABLE"} for c in records
        ],
    }
    assert submit(client, (dataset_id, payload)).status_code == 200
    payload["split"] = "prod"
    assert submit(client, (dataset_id, payload)).status_code == 422
    assert submit(client, ("missing", payload)).status_code == 404


def test_cancelled_batch_preserves_every_id_as_error(client, submission_data):
    dataset_id, payload = submission_data
    repo, work = client.app.state.repository, client.app.state.services.work_store
    batch = work.create_batch(
        BatchRequest(
            conversation_ids=[c.id for c in repo.export_dataset(dataset_id).conversations],
            checklist_id="freedom-broker",
            checklist_version=1,
        ),
        100,
    )
    work.cancel_pending(batch.id)
    payload.pop("items")
    payload["batch_id"] = batch.id
    response = submit(client, submission_data)
    assert response.status_code == 200
    rows = response.json()["results"]
    assert len(rows) == 10
    assert all(row["score"] is None and row["criteria"] == [] for row in rows)
    assert all(row["error"] == "BATCH_CANCELLED_BEFORE_START" for row in rows)


def test_cli_check_rejects_tampering_and_protects_inputs(
    client, submission_data, settings, tmp_path
):
    dataset_id, payload = submission_data
    manifest, output = tmp_path / "manifest.json", tmp_path / "results.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    valid = submit(client, submission_data).json()
    for change in ("bool_as_number", "score", "duplicate_key", "invalid_json"):
        document = deepcopy(valid)
        if change == "bool_as_number":
            document["results"][0]["criteria"][0]["value"] = 1
        elif change == "score":
            document["results"][0]["score"]["final_score"] = 99
        text = json.dumps(document)
        if change == "duplicate_key":
            text = text.replace(
                '"schema_version": "1.0"', '"schema_version": "1.0", "schema_version": "1.0"'
            )
        elif change == "invalid_json":
            text += ","
        output.write_text(text, encoding="utf-8")
        assert run_cli(settings, dataset_id, manifest, output, "--check").returncode == 2
        assert output.read_text(encoding="utf-8") == text
    for target in (
        settings.database_path,
        manifest,
        settings.database_path.with_suffix(".sqlite3-wal"),
    ):
        assert run_cli(settings, dataset_id, manifest, target, "--overwrite").returncode == 2
    assert client.get("/health").status_code == 200
