from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "test.sqlite3", organizer_api_url="")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def dataset():
    # Entirely synthetic test input, unrelated to organizer conversations.
    return {"name": "Synthetic", "group_id": "TEST", "records": [{
        "conversation_id": "synthetic-1", "language": "RU", "channel": "agent chat",
        "transcript": [
            {"timestamp": "09:00:00", "speaker": "client", "text": "Здравствуйте! Где инструкция?"},
            {"timestamp": "09:00:03", "speaker": "operator", "text": "Здравствуйте, я Анна. Инструкция в разделе Помощь. До свидания!"},
        ],
    }]}


@pytest.fixture
def conversation(client, dataset):
    response = client.post("/api/v1/datasets", json=deepcopy(dataset))
    assert response.status_code == 201, response.text
    dataset_id = response.json()["id"]
    item = client.get(f"/api/v1/datasets/{dataset_id}/conversations").json()["items"][0]
    return client.get(f"/api/v1/conversations/{item['id']}").json()


@pytest.fixture
def checklist(client):
    return client.get("/api/v1/checklists/freedom-broker/versions/1").json()


@pytest.fixture
def answers(checklist):
    return {"answers": [{
        "criterion_id": c["id"],
        "value": max(c["scale"]) if c["type"] == "scale" else "no" if c["type"] == "flag" else "yes",
        "explanation": "Synthetic test annotation.", "evidence_kind": "present",
        "evidence": [{"message_id": "m2", "quote": "Здравствуйте, я Анна."}],
    } for c in checklist["criteria"]]}


@pytest.fixture
def evaluation(client, conversation, answers):
    response = client.post("/api/v1/evaluations/manual", json={
        "conversation_id": conversation["id"], "author": "test-reviewer", "result": answers,
    })
    assert response.status_code == 201, response.text
    return response.json()
