"""Prepare human annotation drafts and validated SFT data from a local dataset.

All outputs stay under data/ by default; that directory is ignored by Git.
"""

import argparse
import json
import random
from collections import defaultdict
from importlib.resources import files
from pathlib import Path

from backend.errors import InvalidEvaluationResult
from backend.evaluator import EvaluationContext
from backend.models import (
    ChecklistVersion,
    Conversation,
    ConversationInput,
    DatasetImport,
    EvaluationResult,
    Message,
)
from backend.scoring import validate_result

from .prompt import build_prompt

SFTExample = dict[str, list[dict[str, str]]]


def load_dataset(path: Path) -> DatasetImport:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and "isSuccess" in raw:
            if raw["isSuccess"] is not True:
                raise ValueError("Organizer response was unsuccessful")
            raw = {
                "name": "Local annotations",
                "group_id": "DEV",
                "records": raw["data"]["records"],
            }
        return DatasetImport.model_validate(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid dataset JSON; check organizer format and schema") from exc


def default_checklist() -> ChecklistVersion:
    definition = json.loads(
        files("backend").joinpath("default_checklist.json").read_text(encoding="utf-8")
    )
    return ChecklistVersion.model_validate(
        {
            **definition,
            "checklist_id": "freedom-broker",
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )


def prepare(source: Path, out_dir: Path, per_language: int, seed: int) -> int:
    if per_language < 1:
        raise ValueError("per_language must be positive")
    dataset = load_dataset(source)
    checklist = default_checklist()
    groups: dict[str, list[ConversationInput]] = defaultdict(list)
    for record in dataset.records:
        if record.channel == "agent chat":
            groups[record.language].append(record)
    rng = random.Random(seed)
    selected = []
    for language in sorted(groups):
        records = groups[language]
        rng.shuffle(records)
        selected.extend(records[:per_language])
    if not selected:
        raise ValueError("No agent chat records found")
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.glob("*.json")):
        raise ValueError("Draft directory already has JSON files; choose a new directory")
    for index, record in enumerate(selected, 1):
        conversation = Conversation(
            id=record.conversation_id,
            dataset_id="local-labels",
            conversation_id=record.conversation_id,
            language=record.language,
            channel=record.channel,
            transcript=[
                Message(id=f"m{i}", **message.model_dump())
                for i, message in enumerate(record.transcript, 1)
            ],
            audio_url=record.audio_url,
        )
        draft = {
            "conversation": conversation.model_dump(),
            "checklist": checklist.model_dump(),
            "result": {
                "answers": [
                    {
                        "criterion_id": criterion.id,
                        "value": None,
                        "explanation": "",
                        "evidence_kind": "",
                        "evidence": [{"message_id": "", "quote": ""}],
                        "na_reason": "",
                        "needs_review": False,
                        "limitation": "",
                    }
                    for criterion in checklist.criteria
                ]
            },
        }
        path = out_dir / f"{index:03d}-{record.language}.json"
        path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(selected)


def build(drafts_dir: Path, out_dir: Path) -> tuple[int, int]:
    paths = sorted(drafts_dir.glob("*.json"))
    if not paths:
        raise ValueError("No annotation drafts found")
    groups: dict[str, list[SFTExample]] = defaultdict(list)
    for path in paths:
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
            context = EvaluationContext(
                conversation=Conversation.model_validate(draft["conversation"]),
                checklist=ChecklistVersion.model_validate(draft["checklist"]),
            )
            result = EvaluationResult.model_validate(draft["result"])
            validate_result(context.checklist, context.conversation, result)
        except (KeyError, TypeError, ValueError, InvalidEvaluationResult) as exc:
            # Pydantic errors may contain private transcript snippets; omit them.
            raise ValueError(f"Incomplete or invalid annotation in {path.name}") from exc
        groups[context.conversation.language].append(
            {
                "prompt": [{"role": "user", "content": build_prompt(context)}],
                "completion": [
                    {
                        "role": "assistant",
                        "content": result.model_dump_json(exclude_defaults=False),
                    }
                ],
            }
        )
    train: list[SFTExample] = []
    validation: list[SFTExample] = []
    for language in sorted(groups):
        for index, example in enumerate(groups[language]):
            (validation if index % 5 == 4 else train).append(example)
    if not validation:
        raise ValueError("At least five labeled chats in one language are needed for a holdout")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, examples in (("train.jsonl", train), ("validation.jsonl", validation)):
        (out_dir / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in examples),
            encoding="utf-8",
        )
    return len(train), len(validation)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_cmd = sub.add_parser("prepare", help="Select chats and create blank annotation drafts")
    prepare_cmd.add_argument("source", type=Path)
    prepare_cmd.add_argument("--out", type=Path, default=Path("data/labels/drafts"))
    prepare_cmd.add_argument("--per-language", type=int, default=10)
    prepare_cmd.add_argument("--seed", type=int, default=42)
    build_cmd = sub.add_parser("build", help="Validate completed drafts and make SFT JSONL")
    build_cmd.add_argument("--drafts", type=Path, default=Path("data/labels/drafts"))
    build_cmd.add_argument("--out", type=Path, default=Path("data/labels/sft"))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            count = prepare(args.source, args.out, args.per_language, args.seed)
            print(f"Created {count} annotation drafts in {args.out}")
        else:
            train, validation = build(args.drafts, args.out)
            print(f"Validated {train} train and {validation} validation examples in {args.out}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
