"""Contract tests for the Gemini boundary; no external API calls."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.app import create_hosted_app
from ai.gemini import GeminiEvaluator
from backend.errors import DomainError
from backend.evaluator import EvaluationContext, ProviderDeferred
from backend.models import ChecklistVersion, Conversation, Message
from backend.rate_limits import RequestLimiter


@pytest.fixture(autouse=True)
def fast_requests(monkeypatch):
    # Provider contract fixtures should not spend wall time testing pacing.
    # Separate limiter tests use a deterministic clock and real SQLite locking.
    monkeypatch.setattr(
        "ai.gemini.RequestLimiter", lambda: RequestLimiter(requests_per_minute=60000)
    )


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
        assert result.usage is not None
        assert result.usage.calls == 1
        assert result.usage.complete is False
        assert len(result.result.answers) == 8
        prompt = captured["contents"][0]["parts"][0]["text"]
        assert "Инструкция в разделе Помощь" in prompt
        assert "Установление контакта" in prompt
        assert captured["generationConfig"]["responseMimeType"] == "application/json"
        assert "responseJsonSchema" in captured["generationConfig"]

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
            result = await GeminiEvaluator("test-key", client=client).evaluate(
                make_context(checklist)
            )
        assert result.model == "gemini-3.6-flash"
        assert result.calls == 2
        assert result.usage is not None
        assert result.usage.model == "gemini-3.6-flash"
        assert result.usage.calls == 2
        assert result.input_tokens is None
        assert result.output_tokens is None
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


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_timeout_configuration_must_be_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        GeminiEvaluator("test-key", timeout=timeout)
    with pytest.raises(ValueError, match="finite and positive"):
        GeminiEvaluator("test-key", fallback_timeout=timeout)


@pytest.mark.parametrize("model", ["", "name?key=value", "models/name", "name:method", " name"])
def test_model_cannot_change_api_route(model: str) -> None:
    with pytest.raises(ValueError, match="Invalid Gemini model"):
        GeminiEvaluator("test-key", model=model)


def test_failed_primary_tokens_are_not_priced_as_fallback(checklist: dict, answers: dict) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        primary = "gemini-3.8-flash" in request.url.path
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{"text": "invalid JSON" if primary else json.dumps(answers)}]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 40},
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            output = await GeminiEvaluator("test-key", client=client).evaluate(
                make_context(checklist)
            )
        assert output.model == "gemini-3.6-flash"
        assert output.calls == 2
        assert output.usage is not None and not output.usage.complete
        assert output.usage.input_tokens is None
        assert output.usage.output_tokens is None

    asyncio.run(run())


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_error_is_not_retried_or_leaked(checklist: dict, status: int) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text="secret-provider-body test-key")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(DomainError) as caught:
                await GeminiEvaluator("test-key", client=client).evaluate(make_context(checklist))
        assert caught.value.code == "AI_UNAVAILABLE"
        assert "test-key" not in str(caught.value)
        assert "secret-provider-body" not in str(caught.value)
        assert len(requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize("daily", [False, True])
def test_429_pauses_without_retry_or_fallback_and_does_not_leak(checklist, daily):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            429,
            headers={"Retry-After": "600"},
            json={
                "error": {
                    "message": "test-key secret provider message",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": "PerDay" if daily else "PerMinute"}],
                        }
                    ],
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            evaluator = GeminiEvaluator("test-key", client=client)
            with pytest.raises(ProviderDeferred) as first:
                await evaluator.evaluate(make_context(checklist))
            assert first.value.usage.calls == 1
            assert first.value.quota_exhausted is daily
            assert first.value.safe_to_retry and not first.value.provider_not_called
            with pytest.raises(ProviderDeferred) as second:
                await evaluator.evaluate(make_context(checklist))
            assert second.value.provider_not_called and second.value.usage is None
            assert first.value.retry_at == second.value.retry_at
            assert "test-key" not in str(first.value) and "secret" not in str(first.value)
        assert len(calls) == 1

    asyncio.run(run())


def test_quota_after_ambiguous_attempt_is_not_automatically_replayed(checklist):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("ambiguous")
        return httpx.Response(429, headers={"Retry-After": "600"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ProviderDeferred) as caught:
                await GeminiEvaluator("test-key", client=client).evaluate(make_context(checklist))
            assert not caught.value.safe_to_retry
            assert caught.value.usage.calls == 2
            assert not caught.value.provider_not_called

    asyncio.run(run())


def test_every_attempt_including_fallback_passes_request_gate(checklist, answers):
    models = []
    calls = []

    class RecordingGate:
        async def wait(self, model):
            models.append(model)

        async def defer(self, *args):
            raise AssertionError("No 429 in this fixture")

    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(answers)}]}}
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await GeminiEvaluator(
                "test-key", client=client, request_gate=RecordingGate()
            ).evaluate(make_context(checklist))
        assert result.calls == 3
        assert models == ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.6-flash"]

    asyncio.run(run())
