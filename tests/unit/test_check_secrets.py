"""Secret checks fail without echoing credential values."""

from scripts.check_secrets import scan


def test_detects_key_and_env_without_printing_value() -> None:
    fake_key = b"AQ." + b"A" * 50
    findings = scan(".env", b"GEMINI_API_KEY=" + fake_key + b"\n")
    assert len(findings) >= 2
    assert all(fake_key.decode() not in finding for finding in findings)


def test_allows_empty_example() -> None:
    assert scan(".env.example", b"GEMINI_API_KEY=\n") == []
    assert scan("docs/example.md", b"GEMINI_API_KEY=...\n") == []


def test_rejects_paid_gateway_credentials_without_echo() -> None:
    fake_key = b"rs_client_" + b"A" * 42
    findings = scan("config.txt", b"CQA_LLM_API_KEY=" + fake_key + b"\n")
    assert findings and all(fake_key.decode() not in finding for finding in findings)


def test_rejects_worker_credentials_without_echo() -> None:
    fake_key = b"cqa_worker_" + b"A" * 42
    findings = scan("config.txt", b"CQA_LOCAL_AI_KEY=" + fake_key + b"\n")
    assert findings and all(fake_key.decode() not in finding for finding in findings)
