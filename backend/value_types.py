"""Reusable value constraints; validation never rewrites source text."""

from typing import Annotated, Literal

from pydantic import AfterValidator

Language = Literal["RU", "KZ", "EN"]
Channel = Literal["agent chat", "voice call"]
EvidenceKind = Literal["present", "missing", "not_applicable"]


def require_content(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonBlank = Annotated[str, AfterValidator(require_content)]
