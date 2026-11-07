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
