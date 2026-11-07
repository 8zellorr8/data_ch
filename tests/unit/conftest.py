import pytest

from backend.domain import new_evaluation
from backend.models import ChecklistVersion, Conversation, EvaluationResult, ManualMetadata


@pytest.fixture
def domain_conversation(dataset):
    raw = dataset["records"][0]
    return Conversation.model_validate(
        {
            **raw,
            "id": "conversation-1",
            "dataset_id": "dataset-1",
            "audio_url": None,
            "transcript": [
                {**message, "id": f"m{index + 1}"}
                for index, message in enumerate(raw["transcript"])
            ],
        }
    )


@pytest.fixture
def domain_checklist(checklist):
    return ChecklistVersion.model_validate(checklist)


@pytest.fixture
def domain_result(answers):
    return EvaluationResult.model_validate(answers)


@pytest.fixture
def domain_evaluation(domain_conversation, domain_checklist, domain_result):
    return new_evaluation(
        domain_conversation,
        domain_checklist,
        domain_result,
        ManualMetadata(author="QA"),
        record_id="evaluation-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
