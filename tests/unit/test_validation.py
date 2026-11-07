from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.models import (
    AIModelMetadata,
    AppealDecision,
    AppealRequest,
    ChecklistDefinition,
    ConversationInput,
    Criterion,
    DatasetImport,
    ManualEvaluationRequest,
    OrganizerImport,
)


@pytest.mark.parametrize(
    "request_name,field",
    [
        ("dataset", "name"),
        ("dataset", "group_id"),
        ("organizer", "name"),
        ("organizer", "group_id"),
        ("criterion", "name"),
        ("criterion", "rule"),
        ("checklist", "name"),
        ("manual", "author"),
        ("appeal", "author"),
        ("appeal", "comment"),
        ("decision", "reason"),
        ("decision", "reviewer"),
        ("metadata", "model"),
        ("metadata", "prompt_version"),
    ],
)
def test_blank_labels_are_rejected_consistently(dataset, answers, request_name, field):
    criterion = {"id": "x", "name": "X", "rule": "Rule", "type": "binary", "weight": 10}
    cases = {
        "dataset": (DatasetImport, dataset),
        "organizer": (OrganizerImport, {}),
        "criterion": (Criterion, criterion),
        "checklist": (ChecklistDefinition, {"name": "Rubric", "criteria": [criterion]}),
        "manual": (
            ManualEvaluationRequest,
            {"conversation_id": "c1", "author": "QA", "result": answers},
        ),
        "appeal": (AppealRequest, {"criterion_id": "contact", "author": "QA", "comment": "Review"}),
        "decision": (
            AppealDecision,
            {
                "expected_revision": 1,
                "value": "yes",
                "reason": "Review",
                "reviewer": "QA",
                "evidence_kind": "present",
                "evidence": [{"message_id": "m1", "quote": "Text"}],
            },
        ),
        "metadata": (AIModelMetadata, {"model": "test", "prompt_version": "v1", "duration_ms": 0}),
    }
    model, payload = cases[request_name]
    payload = {**deepcopy(payload), field: " \t\n\u00a0 "}
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["msg"] == "Value error, must not be blank"


def test_label_validation_preserves_meaningful_spacing():
    request = AppealRequest(
        criterion_id="contact", author="  QA  ", comment="  First line\nSecond line  "
    )
    assert request.author == "  QA  "
    assert request.comment == "  First line\nSecond line  "


def test_source_transcript_is_preserved_verbatim(dataset):
    raw = dataset["records"][0]
    raw["transcript"][0]["text"] = "  Сәлеметсіз бе!\n  Hello.  "
    raw["transcript"][1]["text"] = " \t "
    conversation = ConversationInput.model_validate(raw)
    assert [m.model_dump() for m in conversation.transcript] == raw["transcript"]
