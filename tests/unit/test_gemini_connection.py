"""Connection command never performs inference without the explicit live flag."""

import json

from ai.check_connection import main


def test_missing_key_has_explicit_failure(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr("sys.argv", ["check_connection"])
    assert main() == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "AI_NOT_CONFIGURED"


def test_configuration_probe_does_not_call_provider(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("CQA_GEMINI_MODEL", "gemini-3.6-flash")
    monkeypatch.setattr("sys.argv", ["check_connection"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("A configuration check must not call Gemini")

    monkeypatch.setattr("ai.gemini.GeminiEvaluator.evaluate", forbidden)
    assert main() == 0
    output = capsys.readouterr().out
    assert "test-key" not in output
    assert json.loads(output) == {
        "ai_configured": True,
        "model": "gemini-3.6-flash",
        "live_checked": False,
    }
