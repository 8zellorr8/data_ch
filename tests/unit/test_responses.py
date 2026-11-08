"""Provider boundary tests: no paid API requests are made by the test suite."""

import asyncio
import copy
import json

import httpx
import pytest

from ai.budget import TokenBudget
from ai.config import build_evaluator
from ai.responses import ResponsesEvaluator
from backend.db import Repository
from backend.errors import DomainError
from backend.evaluator import EvaluationContext, ProviderDeferred
from backend.models import ChecklistVersion, Conversation, Message


@pytest.fixture
def context(dataset, checklist):
    record = dataset["records"][0]
    return EvaluationContext(
        Conversation(
            id="internal",
            dataset_id="synthetic",
            conversation_id=record["conversation_id"],
            language="RU",
            channel="agent chat",
            audio_url=None,
            transcript=[Message(id=f"m{i}", **m) for i, m in enumerate(record["transcript"], 1)],
        ),
        ChecklistVersion.model_validate(checklist),
    )


@pytest.fixture
def provider_answers(answers):
    for answer in answers["answers"]:
        if answer["criterion_id"] == "consultation":
            answer.update(needs_review=True, limitation="Official product reference absent")
    return answers


def envelope(answers, *, usage=True):
    response = {
        "status": "completed",
        "model": "gemini-3.8-flash",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(answers)}],
            }
        ],
    }
    if usage:
        response["usage"] = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    return response


def test_contract_and_repair_preserve_paid_usage(tmp_path, context, provider_answers):
    invalid = copy.deepcopy(provider_answers)
    invalid["answers"][0]["evidence"][0]["quote"] = "Invented quote"
    captured = []

    def respond(request):
        body = json.loads(request.content)
        captured.append(body)
        assert request.url.path == "/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert body["store"] is False
        assert body["max_output_tokens"] == 4096
        assert "верхним ключом answers" in body["input"][0]["content"]
        if len(captured) == 2:
            assert "Quote does not match" in body["input"][-1]["content"]
            assert "Здравствуйте, я Анна" in body["input"][-1]["content"]
        return httpx.Response(
            200, json=envelope(invalid if len(captured) == 1 else provider_answers)
        )

    budget = TokenBudget(tmp_path / "budget.sqlite3")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await ResponsesEvaluator(
                "test-key",
                base_url="https://provider.test/v1",
                model="gemini-3.8-flash",
                budget=budget,
                client=client,
            ).evaluate(context)

    result = asyncio.run(run())
    assert result.calls == 2 and len(result.result.answers) == 8
    assert result.input_tokens == 200 and result.output_tokens == 100
    assert budget.snapshot()["accounted_tokens"] == 300


@pytest.mark.parametrize("mode", ["error_200", "unauthorized", "missing_usage", "wrong_model"])
def test_errors_fail_safely_and_unknown_usage_remains_reserved(
    tmp_path, context, provider_answers, mode
):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if mode == "error_200":
            return httpx.Response(200, json={"error": {"message": "test-key provider details"}})
        if mode == "unauthorized":
            return httpx.Response(401, text="test-key private body")
        body = envelope(provider_answers, usage=mode != "missing_usage")
        if mode == "wrong_model":
            body["model"] = "another-model"
        return httpx.Response(200, json=body)

    budget = TokenBudget(tmp_path / "budget.sqlite3")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            evaluator = ResponsesEvaluator(
                "test-key",
                base_url="https://provider.test/v1",
                model="gemini-3.8-flash",
                budget=budget,
                client=client,
                max_attempts=1,
            )
            if mode == "missing_usage":
                return await evaluator.evaluate(context)
            with pytest.raises(DomainError) as exc:
                await evaluator.evaluate(context)
            assert "test-key" not in str(exc.value)

    asyncio.run(run())
    assert count == 1
    assert budget.snapshot()["uncertain_requests"] == (0 if mode == "wrong_model" else 1)


def test_budget_and_voice_gate_prevent_network_call(tmp_path, context):
    def respond(request):
        pytest.fail("No provider request may be sent")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            evaluator = ResponsesEvaluator(
                "test-key",
                base_url="https://provider.test/v1",
                model="gemini-3.8-flash",
                budget=TokenBudget(tmp_path / "budget.sqlite3", total=100, jury_reserve=90),
                client=client,
            )
            with pytest.raises(DomainError):
                await evaluator.evaluate(context)
            context.conversation.channel = "voice call"
            with pytest.raises(DomainError) as exc:
                await evaluator.evaluate(context)
            assert exc.value.code == "AI_NOT_CONFIGURED"

    asyncio.run(run())


def test_factory_selects_paid_provider_and_shared_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("CQA_LLM_BUDGET_PATH", str(tmp_path / "budget.sqlite3"))
    assert isinstance(build_evaluator(), ResponsesEvaluator)
    assert build_evaluator().budget.total == 2_000_000
    assert build_evaluator().budget.jury_reserve == 0


def test_paid_factory_requires_explicit_enable(monkeypatch, tmp_path, context):
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("CQA_LLM_API_ENABLED", "0")
    path = tmp_path / "no-requests.sqlite3"
    monkeypatch.setenv("CQA_LLM_BUDGET_PATH", str(path))
    evaluator = build_evaluator()
    assert not evaluator.configured
    with pytest.raises(DomainError):
        asyncio.run(evaluator.evaluate(context))
    assert not path.exists()


def test_paid_429_survives_fresh_factory_without_a_second_request(monkeypatch, tmp_path, context):
    database = tmp_path / "shared.sqlite3"
    Repository(database).initialize()
    monkeypatch.setenv("CQA_DATABASE_PATH", str(database))
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_ENABLED", "1")
    monkeypatch.setenv("CQA_LLM_API_KEY", "test-key")
    monkeypatch.setenv("CQA_LLM_BASE_URL", "https://provider.test/v1")
    monkeypatch.setenv("CQA_LLM_BUDGET_PATH", str(tmp_path / "budget.sqlite3"))
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = build_evaluator(persistent=True)
            second = build_evaluator(persistent=True)
            assert isinstance(first, ResponsesEvaluator) and isinstance(second, ResponsesEvaluator)
            first._client = second._client = client
            with pytest.raises(ProviderDeferred) as refusal:
                await first.evaluate(context)
            assert refusal.value.safe_to_retry
            snapshot = first.budget.snapshot()
            with pytest.raises(ProviderDeferred) as blocked:
                await second.evaluate(context)
            assert blocked.value.provider_not_called
            assert second.budget.snapshot() == snapshot
            assert snapshot["requests"] == snapshot["uncertain_requests"] == 1

    asyncio.run(run())
    assert len(calls) == 1


def test_paid_429_after_unknown_response_does_not_allow_automatic_replay(
    monkeypatch, tmp_path, context
):
    count = 0

    async def no_wait(_):
        pass

    monkeypatch.setattr("ai.responses.asyncio.sleep", no_wait)

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("Unknown result")
        return httpx.Response(429, headers={"Retry-After": "60"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            evaluator = ResponsesEvaluator(
                "test-key",
                base_url="https://provider.test/v1",
                model="gemini-3.8-flash",
                budget=TokenBudget(tmp_path / "budget.sqlite3"),
                client=client,
            )
            with pytest.raises(ProviderDeferred) as refusal:
                await evaluator.evaluate(context)
            assert not refusal.value.safe_to_retry
            assert evaluator.budget.snapshot()["uncertain_requests"] == 2

    asyncio.run(run())
    assert count == 2
