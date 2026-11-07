"""Only integration tests start FastAPI or create a SQLite database."""

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
def evaluation(client, conversation, answers):
    response = client.post(
        "/api/v1/evaluations/manual",
        json={
            "conversation_id": conversation["id"],
            "author": "test-reviewer",
            "result": answers,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
