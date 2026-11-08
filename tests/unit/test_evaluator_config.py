"""Merging the local evaluator selector must preserve durable cloud cooldowns."""

import asyncio
from time import time

import pytest

from ai.config import build_evaluator
from ai.gemini import GeminiEvaluator
from backend.db import Repository
from backend.evaluator import ProviderDeferred


def test_factory_preserves_cooldown_across_fresh_evaluators(monkeypatch, tmp_path):
    database = tmp_path / "shared.sqlite3"
    Repository(database).initialize()
    monkeypatch.setenv("CQA_DATABASE_PATH", str(database))
    monkeypatch.setenv("CQA_AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    first = build_evaluator(persistent=True)
    second = build_evaluator(persistent=True)
    assert isinstance(first, GeminiEvaluator) and isinstance(second, GeminiEvaluator)
    deadline = time() + 60

    async def run():
        await first._request_gate.defer(first.model, deadline, True)
        with pytest.raises(ProviderDeferred) as refusal:
            await second._request_gate.wait(second.model)
        assert refusal.value.retry_at == deadline
        assert refusal.value.quota_exhausted
        assert refusal.value.provider_not_called

    asyncio.run(run())
