"""Contract tests for the Gemini boundary; no external API calls."""

import asyncio
import json

import httpx
import pytest

from ai.gemini import GeminiEvaluator
from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, Message


def make_context(checklist: dict) -> EvaluationContext:
    conversation = Conversation(
        id="internal-1",
        dataset_id="dataset-1",
        conversation_id="synthetic-1",
        language="RU",
        channel="agent chat",
        transcript=[
            Message(id="m1", timestamp="00:01", speaker="client", text="Где инструкция?"),
            Message(
                id="m2",
                timestamp="00:02",
                speaker="operator",
                text="Здравствуйте, я Анна. Инструкция в разделе Помощь. До свидания!",
            ),
        ],
        audio_url=None,
    )
    return EvaluationContext(conversation, ChecklistVersion.model_validate(checklist))


def test_gemini_returns_valid_contract(checklist: dict, answers: dict) -> None:
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == "test-key"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(answers)}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 40,
                    "thoughtsTokenCount": 10,
                },
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await GeminiEvaluator("test-key", client=client).evaluate(
                make_context(checklist)
            )
        assert result.model == "gemini-3.8-flash"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert len(result.result.answers) == 8
        prompt = captured["contents"][0]["parts"][0]["text"]
        assert "Инструкция в разделе Помощь" in prompt
        assert "Установление контакта" in prompt
        assert captured["generationConfig"]["responseMimeType"] == "application/json"

    asyncio.run(run())


def test_gemini_rejects_invented_quote(checklist: dict, answers: dict) -> None:
    answers["answers"][0]["evidence"][0]["quote"] = "Этого в диалоге нет"

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(answers)}]},
                    }
                ]
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(DomainError) as exc:
                await GeminiEvaluator("test-key", client=client).evaluate(make_context(checklist))
        assert exc.value.code == "AI_INVALID_RESULT"

    asyncio.run(run())


def test_voice_call_requires_explicit_opt_in(checklist: dict) -> None:
    context = make_context(checklist)
    context.conversation.channel = "voice call"
    with pytest.raises(DomainError) as exc:
        asyncio.run(GeminiEvaluator("test-key").evaluate(context))
    assert exc.value.code == "AI_NOT_CONFIGURED"
