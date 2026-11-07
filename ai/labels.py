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

from .prompt import PROMPT_VERSION, build_prompt

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


def prepare(
    source: Path,
    out_dir: Path,
    per_language: int,
    seed: int,
    selected_ids: list[str] | None = None,
) -> int:
    if per_language < 1:
        raise ValueError("per_language must be positive")
    dataset = load_dataset(source)
    checklist = default_checklist()
    if selected_ids:
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("Duplicate conversation IDs were selected")
        by_id = {record.conversation_id: record for record in dataset.records}
        if any(cid not in by_id for cid in selected_ids):
            raise ValueError("Selected conversation ID is absent from the dataset")
        selected = [by_id[cid] for cid in selected_ids]
    else:
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
            "annotation_status": "draft",
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


def build(
    drafts_dir: Path,
    out_dir: Path,
    allow_provisional: bool = False,
    per_criterion: bool = False,
) -> tuple[int, int]:
    paths = sorted(drafts_dir.glob("*.json"))
    if not paths:
        raise ValueError("No annotation drafts found")
    groups: dict[str, list[tuple[str, list[SFTExample], list[str]]]] = defaultdict(list)
    status_counts: dict[str, int] = defaultdict(int)
    for path in paths:
        try:
            draft = json.loads(path.read_text(encoding="utf-8"))
            status = draft.get("annotation_status")
            if status != "reviewed" and not (allow_provisional and status == "provisional"):
                raise ValueError("Annotation has not been reviewed")
            context = EvaluationContext(
                conversation=Conversation.model_validate(draft["conversation"]),
                checklist=ChecklistVersion.model_validate(draft["checklist"]),
            )
            result = EvaluationResult.model_validate(draft["result"])
            validate_result(context.checklist, context.conversation, result)
        except (KeyError, TypeError, ValueError, InvalidEvaluationResult) as exc:
            # Pydantic errors may contain private transcript snippets; omit them.
            raise ValueError(f"Incomplete or invalid annotation in {path.name}") from exc
        bundles = []
        criterion_ids = []
        selections = (
            [[criterion] for criterion in context.checklist.criteria]
            if per_criterion
            else [context.checklist.criteria]
        )
        for selection in selections:
            selected_ids = {criterion.id for criterion in selection}
            selected_context = EvaluationContext(
                context.conversation, context.checklist.model_copy(update={"criteria": selection})
            )
            selected_result = EvaluationResult(
                answers=[answer for answer in result.answers if answer.criterion_id in selected_ids]
            )
            bundles.append(
                {
                    "prompt": [{"role": "user", "content": build_prompt(selected_context)}],
                    "completion": [
                        {
                            "role": "assistant",
                            "content": selected_result.model_dump_json(exclude_defaults=False),
                        }
                    ],
                }
            )
            criterion_ids.append(selection[0].id if per_criterion else "")
        groups[context.conversation.language].append(
            (context.conversation.conversation_id, bundles, criterion_ids)
        )
        status_counts[status] += 1
    train: list[SFTExample] = []
    validation: list[SFTExample] = []
    train_ids: list[str] = []
    validation_ids: list[str] = []
    train_row_ids: list[str] = []
    validation_row_ids: list[str] = []
    train_criterion_ids: list[str] = []
    validation_criterion_ids: list[str] = []
    for language in sorted(groups):
        for index, (cid, examples, criterion_ids) in enumerate(groups[language]):
            if index % 5 == 4:
                validation.extend(examples)
                validation_ids.append(cid)
                validation_row_ids.extend([cid] * len(examples))
                validation_criterion_ids.extend(criterion_ids)
            else:
                train.extend(examples)
                train_ids.append(cid)
                train_row_ids.extend([cid] * len(examples))
                train_criterion_ids.extend(criterion_ids)
    if not validation:
        raise ValueError("At least five labeled chats in one language are needed for a holdout")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, examples in (("train.jsonl", train), ("validation.jsonl", validation)):
        (out_dir / name).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in examples),
            encoding="utf-8",
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "prompt_version": PROMPT_VERSION,
                "annotation_status_counts": dict(status_counts),
                "train_examples": len(train),
                "validation_examples": len(validation),
                "train_conversation_ids": train_ids,
                "validation_conversation_ids": validation_ids,
                "example_format": "criterion" if per_criterion else "full_checklist",
                "train_example_conversation_ids": train_row_ids,
                "validation_example_conversation_ids": validation_row_ids,
                "train_criterion_ids": train_criterion_ids,
                "validation_criterion_ids": validation_criterion_ids,
            },
            indent=2,
        )
        + "\n",
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
    prepare_cmd.add_argument("--ids", nargs="+", help="Select exact conversation IDs")
    build_cmd = sub.add_parser("build", help="Validate completed drafts and make SFT JSONL")
    build_cmd.add_argument("--drafts", type=Path, default=Path("data/labels/drafts"))
    build_cmd.add_argument("--out", type=Path, default=Path("data/labels/sft"))
    build_cmd.add_argument(
        "--per-criterion",
        action="store_true",
        help="One criterion per example; split by whole chat",
    )
    build_cmd.add_argument(
        "--allow-provisional",
        action="store_true",
        help="Experimental training with unreviewed labels",
    )
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            count = prepare(args.source, args.out, args.per_language, args.seed, args.ids)
            print(f"Created {count} annotation drafts in {args.out}")
        else:
            train, validation = build(
                args.drafts, args.out, args.allow_provisional, args.per_criterion
            )
            print(f"Validated {train} train and {validation} validation examples in {args.out}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
