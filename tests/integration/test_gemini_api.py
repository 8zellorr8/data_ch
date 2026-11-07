"""Real adapter and SQLite through HTTP; provider responses are synthetic mocks."""

import json
from time import monotonic, sleep

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.gemini import GeminiEvaluator
from backend.main import create_app
from backend.rate_limits import RequestLimiter


@pytest.mark.parametrize(
    ("language", "channel", "quote"),
    [
        ("RU", "agent chat", "Здравствуйте!"),
        ("KZ", "agent chat", "Сәлеметсіз бе!"),
        ("EN", "agent chat", "Hello!"),
        ("RU", "voice call", "Здравствуйте!"),
    ],
)
def test_custom_rubric_single_replay_and_batch(settings, dataset, language, channel, quote):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        prompt = body["contents"][0]["parts"][0]["text"]
        inputs = json.loads(prompt.split("ДАННЫЕ:\n", 1)[1])
        assert inputs["language"] == language and inputs["channel"] == channel
        assert [c["id"] for c in inputs["checklist"]["criteria"]] == ["custom_quality"]
        if channel == "voice call":
            assert inputs["checklist"]["criteria"][0]["call_rule"] == "Только текст звонка."
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "answers": [
                                                {
                                                    "criterion_id": "custom_quality",
                                                    "value": 2,
                                                    "explanation": "Synthetic provider fixture, not a quality label.",
                                                    "evidence_kind": "present",
                                                    "evidence": [
                                                        {"message_id": "m2", "quote": quote}
                                                    ],
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
            },
        )

    transport = httpx.MockTransport(respond)
    evaluator = GeminiEvaluator(
        "test-key",
        model="gemini-3.6-flash",
        allow_voice_calls=True,
        request_gate=RequestLimiter(requests_per_minute=60000),
        client=httpx.AsyncClient(transport=transport),
    )
    dataset["records"][0].update(language=language, channel=channel)
    dataset["records"][0]["transcript"][1]["text"] = quote
    with TestClient(create_app(settings, evaluator)) as client:
        assert client.get("/health").json()["ai_configured"] is True
        imported = client.post("/api/v1/datasets", json=dataset).json()
        cid = client.get(f"/api/v1/datasets/{imported['id']}/conversations").json()["items"][0][
            "id"
        ]
        rubric = client.post(
            "/api/v1/checklists",
            json={
                "name": "Custom rubric",
                "criteria": [
                    {
                        "id": "custom_quality",
                        "name": "Quality",
                        "type": "scale",
                        "weight": 10,
                        "scale": [0, 2, 4],
                        "rule": "Проверь понятность ответа.",
                        "call_rule": "Только текст звонка.",
                    }
                ],
            },
        ).json()
        payload = {
            "conversation_id": cid,
            "checklist_id": rubric["checklist_id"],
            "checklist_version": 1,
        }
        headers = {"Idempotency-Key": "gemini-single"}
        response = client.post("/api/v1/evaluations", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["origin"] == "ai" and record["score"]["total"] == 50
        assert record["metadata"]["audio_analyzed"] is False
        assert record["metadata"]["model"] == "gemini-3.6-flash"
        assert record["original"] == record["final"]
        assert client.post("/api/v1/evaluations", json=payload, headers=headers).json() == record
        assert len(calls) == 1
        batch = client.post(
            "/api/v1/evaluation-batches",
            json={
                "conversation_ids": [cid],
                "checklist_id": rubric["checklist_id"],
                "checklist_version": 1,
            },
        )
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            state = client.get(f"/api/v1/evaluation-batches/{batch_id}").json()
            if state["completed"]:
                break
            sleep(0.01)
        assert state["completed"] and state["items"][0]["status"] == "succeeded"
        assert len(calls) == 2
        saved_id = state["items"][0]["evaluation_id"]
    with TestClient(create_app(settings)) as restarted:
        saved = restarted.get(f"/api/v1/evaluations/{saved_id}").json()
        assert saved["origin"] == "ai" and saved["score"]["total"] == 50
        assert len(calls) == 2
