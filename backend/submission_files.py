"""Shared strict JSON input and atomic publication for both submission CLIs."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 16 * 1024 * 1024


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def parse_json(raw: bytes) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("JSON exceeds 16 MiB")
    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant
    )


def read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        return parse_json(stream.read(MAX_JSON_BYTES + 1))


def publish(path: Path, text: str, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((text + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic publication without a check-then-write overwrite race.
            os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
