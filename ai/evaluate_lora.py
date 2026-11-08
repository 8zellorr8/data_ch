"""Evaluate a local LoRA adapter on the held-out annotated chats."""

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from backend.errors import DomainError, InvalidEvaluationResult
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, EvaluationResult
from backend.scoring import calculate, validate_result

from .local_lora import LocalLoRAEvaluator, LocalLoRAFailure


def evaluate_by_criterion(
    args: argparse.Namespace, manifest: dict[str, Any], drafts: dict[str, Any]
) -> None:
    compact = manifest.get("example_format") == "compact_dialogue"
    evaluator = LocalLoRAEvaluator(
        args.adapter,
        device=args.device,
        max_new_tokens=args.max_new_tokens if compact else 512,
        timeout=150,
    )
    compared: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    errors = []
    outcomes = []
    generations = []
    na_counts = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    exact_quotes = 0
    cold_started = time.perf_counter()
    evaluator.warmup()
    cold_start_seconds = round(time.perf_counter() - cold_started, 2)
    for cid in manifest["validation_conversation_ids"]:
        draft = drafts[cid]
        context = EvaluationContext(
            Conversation.model_validate(draft["conversation"]),
            ChecklistVersion.model_validate(draft["checklist"]),
        )
        reference = EvaluationResult.model_validate(draft["result"])
        started = time.perf_counter()
        try:
            output, texts = evaluator.evaluate_with_generations(context)
        except DomainError as exc:
            generations.append(
                {
                    "conversation_id": cid,
                    "texts": exc.generations if isinstance(exc, LocalLoRAFailure) else [],
                }
            )
            outcomes.append({"conversation_id": cid, "valid": False, "error_code": exc.code})
            print(f"{cid}: {exc.code}", flush=True)
            continue
        seconds = round(time.perf_counter() - started, 2)
        generations.append({"conversation_id": cid, "texts": texts})
        expected = {answer.criterion_id: answer.value for answer in reference.answers}
        predicted = {answer.criterion_id: answer.value for answer in output.result.answers}
        for criterion, value in predicted.items():
            compared[criterion] += 1
            matches[criterion] += int(value == expected[criterion])
            if value == "na" and expected[criterion] == "na":
                na_counts["true_positive"] += 1
            elif value == "na":
                na_counts["false_positive"] += 1
            elif expected[criterion] == "na":
                na_counts["false_negative"] += 1
        exact_quotes += sum(len(answer.evidence) for answer in output.result.answers)
        actual_score = calculate(context.checklist, output.result).total
        reference_score = calculate(context.checklist, reference).total
        if actual_score is not None and reference_score is not None:
            errors.append(abs(actual_score - reference_score))
        outcomes.append(
            {
                "conversation_id": cid,
                "valid": True,
                "seconds": seconds,
                "predicted": predicted,
                "reference": expected,
                "predicted_score": actual_score,
                "reference_score": reference_score,
                "language": context.conversation.language,
                "channel": context.conversation.channel,
            }
        )
        print(f"{cid}: valid, {seconds}s", flush=True)
    report = {
        "adapter": str(args.adapter),
        "selected": len(outcomes),
        "valid": sum(item["valid"] for item in outcomes),
        "criterion_matches": {
            cid: {"matches": matches[cid], "compared": count} for cid, count in compared.items()
        },
        "score_mae": sum(errors) / len(errors) if errors else None,
        "na_counts": na_counts,
        "validated_exact_quotes": exact_quotes,
        "cold_start_seconds": cold_start_seconds,
        "reference_status": manifest.get("annotation_status_counts", {}),
        "device": evaluator.device,
        "example_format": manifest.get("example_format", "criterion"),
        "outcomes": outcomes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output.with_suffix(".generations.json").write_text(
        json.dumps(generations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Valid results: {report['valid']}/{report['selected']}; report: {args.output}")


def extract_json(text: str) -> str:
    # Qwen3 can emit a thinking section before its final answer.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("No JSON object in generation")
    return text[start : end + 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--drafts", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/lora/seed-evaluation.json"))
    parser.add_argument("--max-new-tokens", type=int, default=2500)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    args = parser.parse_args()
    if not args.adapter.is_dir() or not args.validation.is_file():
        parser.error("Adapter and validation data must exist")
    manifest_path = args.validation.parent / "manifest.json"
    if not manifest_path.is_file():
        parser.error("Validation manifest is required to identify held-out chats")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ids = manifest["validation_conversation_ids"]
    rows = [json.loads(line) for line in args.validation.read_text(encoding="utf-8").splitlines()]
    if len(manifest.get("validation_example_conversation_ids", ids)) != len(rows):
        parser.error("Validation rows and conversation IDs do not match")
    drafts = {}
    for path in args.drafts.glob("*.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        drafts[draft["conversation"]["conversation_id"]] = draft
    if any(cid not in drafts for cid in ids):
        parser.error("A held-out conversation is missing from the drafts directory")
    if manifest.get("example_format") in {"criterion", "compact_dialogue"}:
        evaluate_by_criterion(args, manifest, drafts)
        return
    try:
        import torch  # type: ignore[import-not-found]
        from peft import PeftConfig, PeftModel  # type: ignore[import-not-found]
        from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]
    except ImportError as exc:
        parser.error(f"Install optional LoRA dependencies first: {exc.name}")
    use_cuda = args.device == "cuda" or (args.device == "auto" and torch.cuda.is_available())
    if use_cuda and not torch.cuda.is_available():
        parser.error("CUDA was requested but this PyTorch build cannot use it")
    device = "cuda" if use_cuda else "cpu"
    torch.set_num_threads(4)
    dtype = (
        torch.bfloat16
        if use_cuda and torch.cuda.is_bf16_supported()
        else torch.float16
        if use_cuda
        else torch.float32
    )
    config = PeftConfig.from_pretrained(args.adapter)
    tokenizer = AutoTokenizer.from_pretrained(args.adapter)
    base = AutoModelForCausalLM.from_pretrained(
        config.base_model_name_or_path, dtype=dtype, attn_implementation="sdpa"
    )
    model = PeftModel.from_pretrained(base, args.adapter).to(device)
    model.eval()
    model.config.use_cache = True
    totals: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    score_errors: list[float] = []
    outcomes = []
    generations = []
    for cid, row in zip(ids, rows, strict=True):
        draft = drafts[cid]
        conversation = Conversation.model_validate(draft["conversation"])
        checklist = ChecklistVersion.model_validate(draft["checklist"])
        reference = EvaluationResult.model_validate(draft["result"])
        encoded = tokenizer.apply_chat_template(
            row["prompt"],
            tokenize=True,
            add_generation_prompt=True,
            enable_thinking=False,
            return_tensors="pt",
        ).to(device)
        started = time.perf_counter()
        with torch.inference_mode():
            generated = model.generate(
                **encoded,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        output = tokenizer.decode(
            generated[0][encoded["input_ids"].shape[-1] :], skip_special_tokens=True
        )
        seconds = round(time.perf_counter() - started, 2)
        generations.append({"conversation_id": cid, "text": output})
        try:
            result = EvaluationResult.model_validate_json(extract_json(output))
            validate_result(checklist, conversation, result)
        except (ValueError, ValidationError, InvalidEvaluationResult):
            outcomes.append({"conversation_id": cid, "valid": False, "seconds": seconds})
            print(f"{cid}: invalid structure or citation, {seconds}s", flush=True)
            continue
        expected = {a.criterion_id: a.value for a in reference.answers}
        for answer in result.answers:
            totals[answer.criterion_id] += 1
            matches[answer.criterion_id] += int(answer.value == expected[answer.criterion_id])
        actual_score = calculate(checklist, result).total
        reference_score = calculate(checklist, reference).total
        if actual_score is not None and reference_score is not None:
            score_errors.append(abs(actual_score - reference_score))
        outcomes.append(
            {
                "conversation_id": cid,
                "valid": True,
                "seconds": seconds,
                "predicted": {a.criterion_id: a.value for a in result.answers},
                "reference": expected,
                "predicted_score": actual_score,
                "reference_score": reference_score,
            }
        )
        print(f"{cid}: valid, {seconds}s", flush=True)
    report = {
        "adapter": str(args.adapter),
        "selected": len(rows),
        "valid": sum(item["valid"] for item in outcomes),
        "criterion_matches": {
            criterion: {"matches": matches[criterion], "compared": count}
            for criterion, count in totals.items()
        },
        "score_mae": sum(score_errors) / len(score_errors) if score_errors else None,
        "outcomes": outcomes,
        "reference_status": manifest.get("annotation_status_counts", {}),
        "device": device,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output.with_suffix(".generations.json").write_text(
        json.dumps(generations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Valid results: {report['valid']}/{report['selected']}; report: {args.output}")


if __name__ == "__main__":
    main()
