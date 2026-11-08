"""Reject tracked environment files and recognizable API keys without printing secrets."""

import argparse
import re
import subprocess
from pathlib import Path

KEY_PATTERNS = (
    re.compile(rb"AQ\.[A-Za-z0-9_-]{30,}"),
    re.compile(rb"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(rb"rs_client_[A-Za-z0-9_-]{20,}"),
    re.compile(rb"cqa_worker_[A-Za-z0-9_-]{20,}"),
)
ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:GEMINI_API_KEY|CQA_LLM_API_KEY|CQA_LOCAL_AI_KEY)[ \t]*=[ \t]*([^\s#]+)"
)
PLACEHOLDERS = {b"...", b"<key>", b"<your-key>", b"YOUR_KEY", b"test-key"}
ALLOWED_ENV = {".env.example", ".env.docker.example"}


def git_paths(staged: bool) -> list[str]:
    command = (
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]
        if staged
        else ["git", "ls-files", "-z"]
    )
    result = subprocess.run(command, check=True, capture_output=True)
    return [part.decode("utf-8") for part in result.stdout.split(b"\0") if part]


def scan(path: str, data: bytes) -> list[str]:
    findings = []
    name = Path(path).name.lower()
    if (name == ".env" or name.startswith(".env.")) and name not in ALLOWED_ENV:
        findings.append(f"{path}: environment file must not be tracked")
    for pattern in KEY_PATTERNS:
        for match in pattern.finditer(data):
            line = data.count(b"\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: possible API key")
    for match in ASSIGNMENT.finditer(data):
        value = match.group(1).strip(b"\"'")
        if value not in PLACEHOLDERS:
            line = data.count(b"\n", 0, match.start()) + 1
            findings.append(f"{path}:{line}: nonempty API key assignment")
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check the Git index before commit")
    args = parser.parse_args()
    findings = []
    for path in git_paths(args.staged):
        if args.staged:
            data = subprocess.run(
                ["git", "show", f":{path}"], check=True, capture_output=True
            ).stdout
        else:
            data = Path(path).read_bytes()
        findings.extend(scan(path, data))
    if findings:
        for finding in findings:
            print(finding)
        raise SystemExit(1)
    print("No tracked API keys or environment files detected")


if __name__ == "__main__":
    main()
