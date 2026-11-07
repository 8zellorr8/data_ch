"""Pure input fixtures shared by unit and integration tests."""

import json
from importlib.resources import files

import pytest


@pytest.fixture
def dataset():
    # Entirely synthetic test input, unrelated to organizer conversations.
    return {
        "name": "Synthetic",
        "group_id": "TEST",
        "records": [
            {
                "conversation_id": "synthetic-1",
                "language": "RU",
                "channel": "agent chat",
                "transcript": [
                    {
                        "timestamp": "09:00:00",
                        "speaker": "client",
                        "text": "Здравствуйте! Где инструкция?",
                    },
                    {
                        "timestamp": "09:00:03",
                        "speaker": "operator",
                        "text": "Здравствуйте, я Анна. Инструкция в разделе Помощь. До свидания!",
                    },
                ],
            }
        ],
    }


@pytest.fixture
def checklist():
    definition = json.loads(
        files("backend").joinpath("default_checklist.json").read_text(encoding="utf-8")
    )
    return {
        **definition,
        "checklist_id": "freedom-broker",
        "version": 1,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def answers(checklist):
    return {
        "answers": [
            {
                "criterion_id": c["id"],
                "value": max(c["scale"])
                if c["type"] == "scale"
                else "no"
                if c["type"] == "flag"
                else "yes",
                "explanation": "Synthetic test annotation.",
                "evidence_kind": "present",
                "evidence": [{"message_id": "m2", "quote": "Здравствуйте, я Анна."}],
            }
            for c in checklist["criteria"]
        ]
    }
