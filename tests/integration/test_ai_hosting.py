"""The configured AI factory must expose API routes before its frontend mount."""

from pathlib import Path

from fastapi.testclient import TestClient

from ai.app import create_hosted_app


def test_ai_status_is_not_shadowed_by_frontend(monkeypatch, tmp_path):
    monkeypatch.setenv("CQA_FRONTEND_PATH", str(Path(__file__).resolve().parents[2] / "frontend"))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_ENABLED", "0")
    monkeypatch.setenv("CQA_LLM_BUDGET_PATH", str(tmp_path / "budget.sqlite3"))
    monkeypatch.setenv("CQA_BACKUP_DIRECTORY", "")
    monkeypatch.setenv("CQA_REQUIRE_LOCAL_SQLITE", "0")
    with TestClient(create_hosted_app()) as client:
        assert "Replimetr" in client.get("/").text
        response = client.get("/api/v1/ai/status")
        assert response.status_code == 200
        assert response.json()["provider"] == "openai-compatible"
        assert response.json()["configured"] is False
        assert "/api/v1/ai/status" in client.get("/openapi.json").json()["paths"]
        assert client.get("/.env").status_code == 404
