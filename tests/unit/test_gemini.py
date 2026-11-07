"""Contract tests for the Gemini boundary; no external API calls."""

import asyncio
import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.app import create_hosted_app
from ai.gemini import GeminiEvaluator
from ai.prompt import build_gemini_prompt, review_requirements
from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, Message


@pytest.fixture(autouse=True)
def provider_reference_limitation(answers: dict) -> None:
    # This evaluator receives no official product reference in EvaluationContext.
    for answer in answers["answers"]:
        if answer["criterion_id"] == "consultation":
            answer.update(needs_review=True, limitation="Product reference not supplied")


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
        assert result.model == "gemini-3.5-flash-lite"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.usage is not None
        assert result.usage.calls == 1
        assert result.usage.complete is False
        assert len(result.result.answers) == 8
        prompt = captured["contents"][0]["parts"][0]["text"]
        assert "Инструкция в разделе Помощь" in prompt
        assert "Установление контакта" in prompt
        assert captured["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in captured["generationConfig"]
        assert captured["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "medium"

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


def test_gemini_repairs_invalid_quote_with_source_feedback(checklist: dict, answers: dict) -> None:
    requests = []
    invalid = copy.deepcopy(answers)
    invalid["answers"][0]["evidence"][0]["quote"] = "Invented quote"

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 2:
            feedback = body["contents"][-1]["parts"][0]["text"]
            assert "Quote does not match" in feedback
            assert "Здравствуйте, я Анна" in feedback
            assert "test-key" not in feedback
        result = invalid if len(requests) == 1 else answers
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}
                ]
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            output = await GeminiEvaluator(
                "test-key", model="gemini-3.5-flash-lite", fallback_model=None, client=client
            ).evaluate(make_context(checklist))
        assert output.calls == 2
        assert output.result.answers[0].evidence[0].quote == "Здравствуйте, я Анна."
        branches = requests[0]["generationConfig"]["responseJsonSchema"]["properties"]["answers"][
            "items"
        ]["anyOf"]
        for branch in branches:
            properties = branch["properties"]
            values = properties["value"]["enum"]
            evidence_kinds = properties["evidence_kind"]["enum"]
            assert ("na" in values) == (evidence_kinds == ["not_applicable"])

    asyncio.run(run())


def test_gemini_uses_current_custom_checklist(checklist: dict, answers: dict) -> None:
    criterion = {**checklist["criteria"][0], "id": "custom_contact", "name": "Custom greeting"}
    checklist = {**checklist, "criteria": [criterion]}
    answer = {**answers["answers"][0], "criterion_id": "custom_contact"}

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "Custom greeting" in body["contents"][0]["parts"][0]["text"]
        assert "Для gross" not in body["contents"][0]["parts"][0]["text"]
        schema = body["generationConfig"]["responseJsonSchema"]
        assert schema["properties"]["answers"]["minItems"] == 1
        assert schema["properties"]["answers"]["items"]["properties"]["criterion_id"]["enum"] == [
            "custom_contact"
        ]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps({"answers": [answer]})}]},
                    }
                ]
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            output = await GeminiEvaluator("test-key", client=client).evaluate(
                make_context(checklist)
            )
        assert [item.criterion_id for item in output.result.answers] == ["custom_contact"]

    asyncio.run(run())


def test_gemini_requires_missing_source_limits_for_calls(checklist: dict, answers: dict) -> None:
    context = make_context(checklist)
    context.conversation.channel = "voice call"
    for answer in answers["answers"]:
        if answer["criterion_id"] == "management":
            answer.update(needs_review=True, limitation="Transcript only; no audio analyzed")
    invalid = copy.deepcopy(answers)
    for answer in invalid["answers"]:
        if answer["criterion_id"] == "consultation":
            answer.update(needs_review=False, limitation="")
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 2:
            assert "Missing required review flag for consultation" in str(body["contents"])
        result = invalid if len(requests) == 1 else answers
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}
                ]
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            output = await GeminiEvaluator(
                "test-key", allow_voice_calls=True, client=client
            ).evaluate(context)
        assert output.calls == 2
        assert output.audio_analyzed is False
        reviewed = {a.criterion_id for a in output.result.answers if a.needs_review}
        assert {"consultation", "management"} <= reviewed

    asyncio.run(run())


def test_modified_rubric_rules_take_priority_over_default_guidance(checklist: dict) -> None:
    context = make_context(checklist)
    for criterion in context.checklist.criteria:
        if criterion.id == "closing":
            criterion.rule = "Оператор подвёл итог. Прощание не требуется в этом чек-листе."
        elif criterion.id == "consultation":
            criterion.rule = "Сравни ответ с официальным FAQ, приведённым в этом критерии."
    prompt = build_gemini_prompt(context)
    assert "Прощание не требуется в этом чек-листе" in prompt
    assert "closing: одного прощания" not in prompt
    assert "consultation" not in review_requirements(context)


@pytest.mark.parametrize("failure", ["timeout", "overloaded"])
def test_gemini_falls_back_and_reports_actual_model(
    checklist: dict, answers: dict, failure: str
) -> None:
    requested = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "gemini-3.8-flash" in str(request.url):
            if failure == "timeout":
                raise httpx.ReadTimeout("primary timed out")
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": json.dumps(answers)}]},
                    }
                ],
                "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 20},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await GeminiEvaluator(
                "test-key",
                model="gemini-3.8-flash",
                fallback_model="gemini-3.6-flash",
                client=client,
            ).evaluate(make_context(checklist))
        assert result.model == "gemini-3.6-flash"
        assert result.calls == 2
        assert result.usage is not None
        assert result.usage.model == "gemini-3.6-flash"
        assert result.usage.calls == 2
        assert result.input_tokens == 90
        assert len(requested) == 2

    asyncio.run(run())


def test_gemini_retries_transient_error_on_single_model(checklist: dict, answers: dict) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
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
            output = await GeminiEvaluator(
                "test-key", model="gemini-3.6-flash", client=client
            ).evaluate(make_context(checklist))
        assert output.model == "gemini-3.6-flash"
        assert output.calls == 2
        assert calls == 2

    asyncio.run(run())


def test_gemini_enforces_wall_clock_deadline(checklist: dict) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(DomainError) as exc:
                await GeminiEvaluator(
                    "test-key",
                    model="gemini-3.6-flash",
                    client=client,
                    fallback_timeout=0.01,
                ).evaluate(make_context(checklist))
        assert exc.value.code == "AI_TIMEOUT"
        assert calls == 2

    asyncio.run(run())


def test_voice_call_requires_explicit_opt_in(checklist: dict) -> None:
    context = make_context(checklist)
    context.conversation.channel = "voice call"
    with pytest.raises(DomainError) as exc:
        asyncio.run(GeminiEvaluator("test-key").evaluate(context))
    assert exc.value.code == "AI_NOT_CONFIGURED"


def test_hosted_factory_uses_gemini_configuration(monkeypatch, tmp_path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("hosted test", encoding="utf-8")
    monkeypatch.setenv("CQA_FRONTEND_PATH", str(frontend))
    monkeypatch.setenv("CQA_DATABASE_PATH", str(tmp_path / "private.sqlite3"))
    monkeypatch.delenv("CQA_BACKUP_DIRECTORY", raising=False)
    monkeypatch.setenv("CQA_REQUIRE_LOCAL_SQLITE", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with TestClient(create_hosted_app()) as client:
        assert client.get("/health").json()["ai_configured"] is True
        assert client.get("/").text == "hosted test"
