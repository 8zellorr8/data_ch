"""Compare Gemini with held-out local labels without publishing conversations."""

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, EvaluationResult
from backend.rate_limits import RequestLimiter
from backend.scoring import calculate, validate_result

from .gemini import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, GeminiEvaluator
from .prompt import PROMPT_VERSION


async def benchmark(args: argparse.Namespace) -> dict[str, object]:
    load_dotenv(".env")
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("Set GEMINI_API_KEY in a local .env file")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    ids = manifest["validation_conversation_ids"]
    drafts = {}
    for path in args.drafts.glob("*.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        drafts[draft["conversation"]["conversation_id"]] = draft
    missing = set(ids) - set(drafts)
    if missing:
        raise ValueError("A held-out annotation file is missing")
    evaluator = GeminiEvaluator(
        key,
        model=os.getenv("CQA_GEMINI_MODEL", DEFAULT_MODEL),
        fallback_model=os.getenv("CQA_GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL) or None,
        thinking_level=os.getenv("CQA_GEMINI_THINKING_LEVEL", "low"),
        timeout=float(os.getenv("CQA_GEMINI_PRIMARY_TIMEOUT_SECONDS", "15")),
        fallback_timeout=float(os.getenv("CQA_GEMINI_FALLBACK_TIMEOUT_SECONDS", "40")),
        request_gate=RequestLimiter(requests_per_minute=float(os.getenv("CQA_GEMINI_RPM", "3"))),
    )
    compared: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    score_errors: list[float] = []
    durations: list[float] = []
    models: Counter[str] = Counter()
    outcomes = []
    for cid in ids:
        draft = drafts[cid]
        if draft.get("annotation_status") != "reviewed" and not args.allow_provisional:
            raise ValueError("Unreviewed labels require --allow-provisional")
        conversation = Conversation.model_validate(draft["conversation"])
        checklist = ChecklistVersion.model_validate(draft["checklist"])
        reference = EvaluationResult.model_validate(draft["result"])
        validate_result(checklist, conversation, reference)
        started = time.perf_counter()
        try:
            output = await evaluator.evaluate(EvaluationContext(conversation, checklist))
        except DomainError as exc:
            outcomes.append({"conversation_id": cid, "valid": False, "error_code": exc.code})
            continue
        elapsed = round(time.perf_counter() - started, 2)
        durations.append(elapsed)
        models[output.model] += 1
        expected = {answer.criterion_id: answer.value for answer in reference.answers}
        predicted = {answer.criterion_id: answer.value for answer in output.result.answers}
        for criterion_id, value in predicted.items():
            compared[criterion_id] += 1
            matches[criterion_id] += int(value == expected[criterion_id])
        actual_score = calculate(checklist, output.result).total
        reference_score = calculate(checklist, reference).total
        if actual_score is not None and reference_score is not None:
            score_errors.append(abs(actual_score - reference_score))
        outcomes.append(
            {
                "conversation_id": cid,
                "valid": True,
                "model": output.model,
                "calls": output.calls,
                "seconds": elapsed,
                "predicted": predicted,
                "reference": expected,
                "predicted_score": actual_score,
                "reference_score": reference_score,
            }
        )
        print(f"{cid}: {output.model}, {elapsed}s, {output.calls} call(s)", flush=True)
    return {
        "prompt_version": PROMPT_VERSION,
        "reference_prompt_version": manifest["prompt_version"],
        "reference_status": manifest["annotation_status_counts"],
        "selected": len(ids),
        "valid": sum(item["valid"] for item in outcomes),
        "models": dict(models),
        "criterion_accuracy": {
            cid: {"matches": matches[cid], "compared": count} for cid, count in compared.items()
        },
        "score_mae": statistics.mean(score_errors) if score_errors else None,
        "median_seconds": statistics.median(durations) if durations else None,
        "outcomes": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, default=Path("data/labels/seed-drafts"))
    parser.add_argument("--manifest", type=Path, default=Path("data/labels/seed-sft/manifest.json"))
    parser.add_argument("--output", type=Path, default=Path("data/labels/gemini-benchmark.json"))
    parser.add_argument("--allow-provisional", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(benchmark(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Valid: {report['valid']}/{report['selected']}; report: {args.output}")


if __name__ == "__main__":
    main()
