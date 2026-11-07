"""Check that local model output obeys the same backend contract as cloud output."""

import asyncio
import json
from pathlib import Path

import pytest

from ai.local_lora import LocalLoRAEvaluator, LocalLoRAFailure
from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, Message


@pytest.mark.parametrize("custom", [False, True])
def test_local_answers_are_assembled_for_current_checklist(
    tmp_path, monkeypatch, dataset, checklist, answers, custom
) -> None:
    if custom:
        checklist["criteria"] = [{**checklist["criteria"][0], "id": "custom_greeting"}]
        answers["answers"] = [{**answers["answers"][0], "criterion_id": "custom_greeting"}]
    record = dataset["records"][0]
    conversation = Conversation(
        **{key: value for key, value in record.items() if key != "transcript"},
        id="internal-1",
        dataset_id="dataset-1",
        audio_url=None,
        transcript=[
            Message(id=f"m{i}", **message) for i, message in enumerate(record["transcript"], 1)
        ],
    )
    context = EvaluationContext(conversation, ChecklistVersion.model_validate(checklist))
    evaluator = LocalLoRAEvaluator(tmp_path)
    evaluator.configured = True
    monkeypatch.setattr(evaluator, "_load", lambda: None)
    by_id = {answer["criterion_id"]: answer for answer in answers["answers"]}

    def generate(batch, remaining):
        assert remaining > 0
        assert all(len(selection.checklist.criteria) == 1 for selection in batch)
        return [
            json.dumps({"answers": [by_id[selection.checklist.criteria[0].id]]})
            for selection in batch
        ], 100

    monkeypatch.setattr(evaluator, "_generate_batch", generate)
    output = asyncio.run(evaluator.evaluate(context))
    assert [answer.criterion_id for answer in output.result.answers] == list(by_id)
    assert output.calls == (1 if custom else 4)
    assert output.audio_analyzed is False

    answers["answers"][0]["evidence"][0]["quote"] = "Invented quote"
    with pytest.raises(LocalLoRAFailure):
        asyncio.run(evaluator.evaluate(context))


def test_missing_local_adapter_has_clear_error(checklist: dict) -> None:
    conversation = Conversation(
        id="i",
        dataset_id="d",
        conversation_id="c",
        language="RU",
        channel="agent chat",
        audio_url=None,
        transcript=[Message(id="m1", timestamp="00:00", speaker="client", text="Test")],
    )
    evaluator = LocalLoRAEvaluator(Path("nonexistent-test-adapter"))
    with pytest.raises(DomainError) as exc:
        asyncio.run(
            evaluator.evaluate(
                EvaluationContext(conversation, ChecklistVersion.model_validate(checklist))
            )
        )
    assert exc.value.code == "AI_NOT_CONFIGURED"
