"""Check the hosted sign-in boundary around the UI, API and paid evaluation routes."""

import json
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai.app import create_hosted_app

USER_ENV = "CQA_HOSTED_HTTP_USER"
PASSWORD_ENV = "CQA_HOSTED_HTTP_PASSWORD"
USER = "judge"
PASSWORD = "demo-password_for-tests"


@pytest.fixture
def hosted_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CQA_FRONTEND_PATH", str(Path(__file__).resolve().parents[2] / "frontend"))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(tmp_path / "application.sqlite3"))
    monkeypatch.setenv("CQA_BACKUP_DIRECTORY", "")
    monkeypatch.setenv("CQA_REQUIRE_LOCAL_SQLITE", "0")
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_ENABLED", "0")
    monkeypatch.delenv(USER_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)


def test_auth_covers_hosted_ui_and_api(hosted_environment, monkeypatch):
    monkeypatch.setenv(USER_ENV, USER)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    with TestClient(create_hosted_app()) as client:
        for path in ("/", "/app.js", "/health", "/docs", "/api/v1/ai/status", "/api/v1/datasets"):
            response = client.get(path)
            assert response.status_code == 401
            assert (
                response.headers["WWW-Authenticate"] == 'Basic realm="Replimetr", charset="UTF-8"'
            )
            assert response.headers["Cache-Control"] == "no-store"
        assert client.post("/api/v1/evaluations", json={}).status_code == 401

        credentials = b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        valid = f"bAsIc {credentials}"
        assert client.get("/health", auth=(USER, "incorrect-password")).status_code == 401
        for invalid in ("Basic %", f"Basic {credentials}=", f"Bearer {credentials}"):
            assert client.get("/health", headers={"Authorization": invalid}).status_code == 401
        duplicate = [("Authorization", valid), ("Authorization", valid)]
        assert client.get("/health", headers=duplicate).status_code == 401

        index = client.get("/", auth=(USER, PASSWORD))
        assert index.status_code == 200 and "Replimetr" in index.text
        assert index.headers["Cache-Control"] == "no-store"
        status = client.get("/api/v1/ai/status", headers={"Authorization": valid})
        assert status.status_code == 200 and status.json()["configured"] is False


def test_unset_hosted_auth_preserves_existing_access(hosted_environment):
    with TestClient(create_hosted_app()) as client:
        assert client.get("/health").status_code == 200


def test_credentialed_cross_origin_writes_are_blocked(hosted_environment, monkeypatch, dataset):
    monkeypatch.setenv(USER_ENV, USER)
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    with TestClient(create_hosted_app()) as client:
        for origin in (
            "https://foreign.test",
            "null",
            "ftp://testserver",
            "https://testserver/",
            "https://testserver?",
        ):
            rejected = client.post(
                "/api/v1/datasets",
                content=json.dumps(dataset),
                auth=(USER, PASSWORD),
                headers={"Origin": origin},
            )
            assert rejected.status_code == 403
            assert rejected.json()["error"]["code"] == "FORBIDDEN_ORIGIN"
        duplicate = [("Origin", "https://testserver"), ("Origin", "https://testserver")]
        assert (
            client.post(
                "/api/v1/datasets", json=dataset, auth=(USER, PASSWORD), headers=duplicate
            ).status_code
            == 403
        )
        for headers in ({"Origin": "https://testserver"}, {}):
            accepted = client.post(
                "/api/v1/datasets", json=dataset, auth=(USER, PASSWORD), headers=headers
            )
            assert accepted.status_code == 201


@pytest.mark.parametrize(
    ("user", "password"),
    [(USER, None), (None, PASSWORD), ("", ""), ("a:b", PASSWORD), (USER, "short")],
)
def test_incomplete_or_invalid_credentials_prevent_startup(
    hosted_environment, monkeypatch, user, password
):
    if user is not None:
        monkeypatch.setenv(USER_ENV, user)
    if password is not None:
        monkeypatch.setenv(PASSWORD_ENV, password)
    with pytest.raises(ValueError, match="valid hosted credentials") as failure:
        create_hosted_app()
    assert PASSWORD not in str(failure.value)
