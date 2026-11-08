"""Exercise the real worker/client wire contract without loading GPU weights."""

import asyncio
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from ai.compact import expand_compact
from ai.http_worker import HTTPWorkerEvaluator
from ai.local_lora import LocalLoRAEvaluator
from ai.worker import create_worker_app
from backend.errors import DomainError
from backend.evaluator import EvaluationContext, EvaluatorOutput
from backend.models import ChecklistVersion, Conversation, Message


@pytest.fixture
def worker_case(checklist, answers):
    context = EvaluationContext(
        Conversation(
            id="internal-1",
            dataset_id="dataset-1",
            conversation_id="synthetic-1",
            language="RU",
            channel="agent chat",
            audio_url=None,
            transcript=[
                Message(id="m1", timestamp="00:00", speaker="client", text="Где инструкция?"),
                Message(
                    id="m2",
                    timestamp="00:01",
                    speaker="operator",
                    text="Здравствуйте! Я Анна. Инструкция в разделе Помощь.",
                ),
            ],
        ),
        ChecklistVersion.model_validate(checklist),
    )
    raw = {
        a["criterion_id"]: {
            "value": a["value"],
            "message_ids": ["m2"],
            "explanation": "Обоснование по диалогу.",
        }
        for a in answers["answers"]
    }
    payload = {
        "conversation": context.conversation.model_dump(mode="json"),
        "checklist": context.checklist.model_dump(mode="json"),
    }

    class FakeLocal:
        configured = True
        local_only = True
        model = "local-test"
        calls = 0
        corrupt = False

        async def evaluate(self, selection):
            self.calls += 1
            if self.corrupt:
                selection.conversation.transcript[1].text = "Fabricated source"
            return EvaluatorOutput(expand_compact(selection, raw), self.model, "test-v1", 100, 20)

    return context, payload, FakeLocal()


def test_authentication_precedes_body_parsing_and_inference(worker_case):
    _, payload, model = worker_case
    with TestClient(create_worker_app(model, api_key="test-key")) as client:
        assert client.post("/v1/evaluate", content=b"invalid-json").status_code == 401
        assert (
            client.post(
                "/v1/evaluate", json=payload, headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        assert model.calls == 0
        response = client.post(
            "/v1/evaluate", json=payload, headers={"Authorization": "bearer test-key"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["usage_is_local"] is True
        assert len(response.json()["result"]["answers"]) == 8
        assert "score" not in response.json()
        assert model.calls == 1


def test_payload_limit_and_reference_injection_rejected(worker_case):
    _, payload, model = worker_case
    headers = {"Authorization": "Bearer test-key"}
    with TestClient(create_worker_app(model, api_key="test-key")) as client:
        assert (
            client.post("/v1/evaluate", content=b"x" * 1_000_001, headers=headers).status_code
            == 413
        )
        assert (
            client.post("/v1/evaluate", json={**payload, "result": {}}, headers=headers).status_code
            == 422
        )
        assert model.calls == 0


def test_worker_checks_result_against_unmodified_source(worker_case):
    _, payload, model = worker_case
    before = deepcopy(payload)
    model.corrupt = True
    with TestClient(create_worker_app(model, api_key="test-key")) as client:
        response = client.post(
            "/v1/evaluate", json=payload, headers={"Authorization": "Bearer test-key"}
        )
        assert response.status_code == 502
    assert payload == before


@pytest.mark.parametrize("invalid_source", ["empty", "duplicate_id", "blank"])
def test_ambiguous_or_empty_sources_never_reach_model(worker_case, invalid_source):
    _, payload, model = worker_case
    messages = payload["conversation"]["transcript"]
    if invalid_source == "empty":
        messages.clear()
    elif invalid_source == "duplicate_id":
        messages[1]["id"] = messages[0]["id"]
    else:
        for message in messages:
            message["text"] = " "
    with TestClient(create_worker_app(model, api_key="test-key")) as client:
        response = client.post(
            "/v1/evaluate", json=payload, headers={"Authorization": "Bearer test-key"}
        )
        assert response.status_code == 422
        assert model.calls == 0


def test_worker_requires_key_and_refuses_cloud_models(worker_case):
    _, _, model = worker_case
    with pytest.raises(RuntimeError, match="CQA_LOCAL_AI_KEY"):
        with TestClient(create_worker_app(model, api_key="")):
            pass
    model.local_only = False
    with pytest.raises(ValueError, match="local inference"):
        create_worker_app(model, api_key="test-key")


def test_backend_client_roundtrip_and_worker_busy(worker_case):
    context, payload, model = worker_case

    async def run():
        application = create_worker_app(model, api_key="test-key")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application)) as client:
            evaluator = HTTPWorkerEvaluator("http://127.0.0.1:8002", "test-key", client=client)
            output = await evaluator.evaluate(context)
            assert output.model == model.model and output.calls == 1
            assert output.output_tokens == 20

            started, release = asyncio.Event(), asyncio.Event()
            original = model.evaluate

            async def blocked(selection):
                started.set()
                await release.wait()
                return await original(selection)

            model.evaluate = blocked
            first = asyncio.create_task(evaluator.evaluate(context))
            await started.wait()
            busy = await client.post(
                "http://127.0.0.1:8002/v1/evaluate",
                json=payload,
                headers={"Authorization": "Bearer test-key"},
            )
            assert busy.status_code == 429
            release.set()
            await first

    asyncio.run(run())


def test_worker_ignores_cloud_provider_selection(monkeypatch):
    monkeypatch.setenv("CQA_AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("CQA_LLM_API_ENABLED", "1")
    application = create_worker_app(api_key="test-key")
    assert isinstance(application.state.evaluator, LocalLoRAEvaluator)


@pytest.mark.parametrize(
    "url",
    ["http://public.example", "https://name:secret@example.com", "https://example.com/?key=secret"],
)
def test_insecure_worker_urls_are_rejected(url):
    with pytest.raises(ValueError):
        HTTPWorkerEvaluator(url, "test-key")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {}, "AI_NOT_CONFIGURED"),
        (429, {}, "AI_BUSY"),
        (504, {}, "AI_TIMEOUT"),
        (502, {"detail": {"code": "AI_INVALID_RESULT"}}, "AI_INVALID_RESULT"),
        (502, {"detail": "invalid metadata"}, "AI_UNAVAILABLE"),
        (500, {}, "AI_UNAVAILABLE"),
    ],
)
def test_worker_error_mapping(worker_case, status, body, expected):
    context, _, _ = worker_case

    async def run():
        transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
        async with httpx.AsyncClient(transport=transport) as client:
            evaluator = HTTPWorkerEvaluator("https://gpu.example", "test-key", client=client)
            with pytest.raises(DomainError) as error:
                await evaluator.evaluate(context)
            assert error.value.code == expected

    asyncio.run(run())


def test_invalid_remote_evidence_and_nonlocal_claim_rejected(worker_case):
    context, payload, model = worker_case

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_worker_app(model, api_key="test-key"))
        ) as worker:
            response = await worker.post(
                "http://127.0.0.1/v1/evaluate",
                json=payload,
                headers={"Authorization": "Bearer test-key"},
            )
            good = response.json()
        for mode in ("quote", "missing_local", "false_local"):
            bad = deepcopy(good)
            if mode == "quote":
                bad["result"]["answers"][0]["evidence"][0]["quote"] = "Fabricated quote"
            elif mode == "missing_local":
                bad.pop("usage_is_local")
            else:
                bad["usage_is_local"] = False
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request, payload=bad: httpx.Response(200, json=payload)
                )
            ) as client:
                evaluator = HTTPWorkerEvaluator("http://127.0.0.1:8002", "test-key", client=client)
                with pytest.raises(DomainError) as error:
                    await evaluator.evaluate(context)
                assert error.value.code == "AI_INVALID_RESULT"

    asyncio.run(run())
