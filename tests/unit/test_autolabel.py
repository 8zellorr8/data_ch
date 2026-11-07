"""CLI integration with the shared adapter's quota contract; no provider calls."""

import argparse
import asyncio
import json
from functools import partial

import httpx

from ai import autolabel
from ai.gemini import GeminiEvaluator


def test_autolabel_stops_at_first_quota_refusal(tmp_path, monkeypatch, dataset):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("CQA_GEMINI_RPM", "3")
    dataset["records"] = [
        {**dataset["records"][0], "conversation_id": f"synthetic-{i}"} for i in range(4)
    ]
    source = tmp_path / "dataset.json"
    source.write_text(json.dumps(dataset), encoding="utf-8")
    calls = []

    def refuse(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"}, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
            monkeypatch.setattr(
                autolabel, "GeminiEvaluator", partial(GeminiEvaluator, client=client)
            )
            return await autolabel.annotate(
                argparse.Namespace(
                    source=source,
                    out=tmp_path / "drafts",
                    model="gemini-test",
                    thinking_level="medium",
                    include_voice=False,
                    diagnostics_dir=None,
                    ids_file=None,
                    balanced_order=False,
                    limit=None,
                    min_interval=0,
                )
            )

    report = asyncio.run(run())
    assert len(calls) == 1
    assert len(report["errors"]) == 1
    assert report["errors"][0]["conversation_id"] == "synthetic-0"
    assert report["stopped_reason"]
    assert report["created"] == report["resumed"] == 0
    assert not list((tmp_path / "drafts").glob("*.json"))
