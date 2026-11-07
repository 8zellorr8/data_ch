"""Student output retrieves source quotes without generating their spelling."""

import copy
import json

import pytest

from ai.compact import compact_schema, expand_compact
from ai.local_lora import LocalLoRAEvaluator, LocalLoRAFailure
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, Message


def make_case(checklist, answers):
    context = EvaluationContext(
        Conversation(
            id="i",
            dataset_id="d",
            conversation_id="synthetic-1",
            language="RU",
            channel="agent chat",
            audio_url=None,
            transcript=[
                Message(id="m1", timestamp="00:00", speaker="client", text="Где инструкция?"),
                Message(
                    id="m2",
                    timestamp="00:01",
                    speaker="operator",
                    text="Здравствуйте, я Анна. Инструкция по ценной бумаги здесь.",
                ),
            ],
        ),
        ChecklistVersion.model_validate(checklist),
    )
    raw = {
        a["criterion_id"]: {
            "value": a["value"],
            "message_ids": ["m2"],
            "explanation": "Обоснование по диалогу.",
        }
        for a in answers["answers"]
    }
    return context, raw


def test_quotes_are_extracted_and_missing_reference_is_marked(checklist, answers):
    context, raw = make_case(checklist, answers)
    raw["identity"]["value"] = "na"
    result = expand_compact(context, raw)
    assert len(result.answers) == 8
    assert all(
        a.evidence[0].quote == context.conversation.transcript[1].text for a in result.answers
    )
    assert (
        next(a for a in result.answers if a.criterion_id == "identity").evidence_kind
        == "not_applicable"
    )
    assert next(a for a in result.answers if a.criterion_id == "consultation").needs_review
    schema = compact_schema(context)
    assert schema["required"] == list(raw)


@pytest.mark.parametrize("mode", ["unknown_message", "missing_criterion", "invented_quote"])
def test_invalid_compact_output_is_rejected(checklist, answers, mode):
    context, raw = make_case(checklist, answers)
    raw = copy.deepcopy(raw)
    if mode == "unknown_message":
        raw["contact"]["message_ids"] = ["m999"]
    elif mode == "missing_criterion":
        raw.pop("contact")
    else:
        raw["contact"]["quote"] = "Invented quotation"
    with pytest.raises(ValueError):
        expand_compact(context, raw)


def test_compact_evaluator_uses_one_generation_and_rejects_bad_ids(
    tmp_path, monkeypatch, checklist, answers
):
    context, raw = make_case(checklist, answers)
    evaluator = LocalLoRAEvaluator(tmp_path)
    evaluator.configured = True
    evaluator._format = "compact_dialogue"
    monkeypatch.setattr(evaluator, "_load", lambda: None)

    def generate(batch, remaining):
        assert len(batch) == 1 and len(batch[0].checklist.criteria) == 8
        return [json.dumps(raw)], 120

    monkeypatch.setattr(evaluator, "_generate_batch", generate)
    output, _ = evaluator.evaluate_with_generations(context)
    assert len(output.result.answers) == 8
    assert output.calls == 1 and output.input_tokens == 120
    assert output.audio_analyzed is False
    raw["contact"]["message_ids"] = ["unknown"]
    with pytest.raises(LocalLoRAFailure):
        evaluator.evaluate_with_generations(context)
