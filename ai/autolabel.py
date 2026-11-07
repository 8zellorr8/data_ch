"""Create resumable, provisional Gemini annotations from an authorized local dataset."""

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from hashlib import sha256
from itertools import zip_longest
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import Conversation, EvaluationResult, Message
from backend.scoring import validate_result

from .gemini import DEFAULT_MODEL, DEFAULT_THINKING_LEVEL, GeminiEvaluator
from .labels import default_checklist, load_dataset


async def annotate(args: argparse.Namespace) -> dict[str, object]:
    run_started = time.perf_counter()
    load_dotenv(".env")
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("Set GEMINI_API_KEY in the local environment")
    source = load_dataset(args.source)
    checklist = default_checklist()

    def record_invalid(context: EvaluationContext, model: str, response: str, reason: str) -> None:
        if args.diagnostics_dir is None:
            return
        args.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (args.diagnostics_dir / f"{uuid4().hex}.json").write_text(
            json.dumps(
                {
                    "conversation_id": context.conversation.conversation_id,
                    "model": model,
                    "reason": reason,
                    "response": response,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    evaluator = GeminiEvaluator(
        key,
        model=args.model,
        fallback_model=None,
        allow_voice_calls=args.include_voice,
        fallback_timeout=40,
        thinking_level=args.thinking_level,
        invalid_result_handler=record_invalid if args.diagnostics_dir is not None else None,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    records = [
        record for record in source.records if record.channel == "agent chat" or args.include_voice
    ]
    if args.ids_file is not None:
        selected_ids = json.loads(args.ids_file.read_text(encoding="utf-8"))
        if not isinstance(selected_ids, list) or any(
            not isinstance(cid, str) for cid in selected_ids
        ):
            raise ValueError("IDs file must contain a JSON array of strings")
        if set(selected_ids) - {record.conversation_id for record in records}:
            raise ValueError("A selected conversation is absent or its channel is disabled")
        records = [record for record in records if record.conversation_id in selected_ids]
    if args.balanced_order:
        buckets = {
            key: [record for record in records if (record.channel, record.language) == key]
            for key in sorted({(record.channel, record.language) for record in records})
        }
        records = [
            record
            for group in zip_longest(*buckets.values())
            for record in group
            if record is not None
        ]
    if args.limit:
        records = records[: args.limit]
    created = 0
    resumed = 0
    errors = []
    consecutive_failures = 0
    stopped_reason = ""
    languages: Counter[str] = Counter()
    last_started = 0.0
    existing_paths = {}
    for existing_path in args.out.glob("*.json"):
        saved = json.loads(existing_path.read_text(encoding="utf-8"))
        cid = saved["conversation"]["conversation_id"]
        if cid in existing_paths:
            raise ValueError("Duplicate saved conversation IDs")
        existing_paths[cid] = existing_path
    for index, record in enumerate(records, 1):
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
        path = existing_paths.get(
            record.conversation_id,
            args.out
            / (
                f"{sha256(record.conversation_id.encode()).hexdigest()[:12]}-{record.language}.json"
            ),
        )
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                existing["conversation"] != conversation.model_dump(mode="json")
                or existing["checklist"] != checklist.model_dump(mode="json")
                or existing.get("teacher_model") != args.model
                or existing.get("prompt_version") != evaluator.prompt_version
                or existing.get("teacher_thinking_level", "low") != args.thinking_level
            ):
                raise ValueError(f"Existing annotation has different inputs: {path.name}")
            result = EvaluationResult.model_validate(existing["result"])
            validate_result(checklist, conversation, result)
            resumed += 1
            languages[record.language] += 1
            continue
        delay = args.min_interval - (time.perf_counter() - last_started)
        if delay > 0:
            await asyncio.sleep(delay)
        last_started = time.perf_counter()
        try:
            output = await evaluator.evaluate(EvaluationContext(conversation, checklist))
        except DomainError as exc:
            errors.append(
                {
                    "conversation_id": record.conversation_id,
                    "error_code": exc.code,
                    "message": exc.message,
                }
            )
            consecutive_failures += 1
            print(f"{index}/{len(records)}: {exc.code}: {exc.message}", flush=True)
            if "HTTP 429" in exc.message or "HTTP 403" in exc.message or "HTTP 401" in exc.message:
                stopped_reason = exc.message
                break
            if consecutive_failures >= 3:
                stopped_reason = "Stopped after three consecutive provider failures"
                break
            continue
        consecutive_failures = 0
        draft = {
            "annotation_status": "provisional",
            "annotation_source": "gemini_teacher_not_human_reviewed",
            "teacher_model": output.model,
            "teacher_thinking_level": args.thinking_level,
            "prompt_version": output.prompt_version,
            "seconds": round(time.perf_counter() - last_started, 2),
            "calls": output.calls,
            "input_tokens": output.input_tokens,
            "output_tokens": output.output_tokens,
            "audio_analyzed": output.audio_analyzed,
            "conversation": conversation.model_dump(mode="json"),
            "checklist": checklist.model_dump(mode="json"),
            "result": output.result.model_dump(mode="json"),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
        created += 1
        languages[record.language] += 1
        print(f"{index}/{len(records)}: saved {path.name}, {output.calls} call(s)", flush=True)
    return {
        "source": str(args.source),
        "model": args.model,
        "thinking_level": args.thinking_level,
        "prompt_version": evaluator.prompt_version,
        "elapsed_seconds": round(time.perf_counter() - run_started, 2),
        "selected": len(records),
        "created": created,
        "resumed": resumed,
        "languages": dict(languages),
        "errors": errors,
        "stopped_reason": stopped_reason,
        "annotation_status": "provisional",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, default=Path("data/labels/teacher-drafts"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids-file", type=Path, help="JSON array of conversation IDs")
    parser.add_argument(
        "--thinking-level", choices=["low", "medium", "high"], default=DEFAULT_THINKING_LEVEL
    )
    parser.add_argument("--include-voice", action="store_true")
    parser.add_argument(
        "--balanced-order", action="store_true", help="Interleave languages and channels"
    )
    parser.add_argument(
        "--min-interval", type=float, default=15, help="Seconds between evaluations"
    )
    parser.add_argument(
        "--diagnostics-dir", type=Path, help="Private directory for invalid provider responses"
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.min_interval < 0:
        parser.error("--min-interval cannot be negative")
    report = asyncio.run(annotate(args))
    report_path = args.out.parent / f"{args.out.name}-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with report_path.with_suffix(".runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False) + "\n")
    print(f"Annotations: {report['created']} new, {report['resumed']} reused; {report_path}")


if __name__ == "__main__":
    main()
