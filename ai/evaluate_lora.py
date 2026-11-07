"""Evaluate a local LoRA adapter on the held-out annotated chats."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from backend.errors import InvalidEvaluationResult
from backend.models import ChecklistVersion, Conversation, EvaluationResult
from backend.scoring import calculate, validate_result


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
    if len(ids) != len(rows):
        parser.error("Validation rows and conversation IDs do not match")
    drafts = {}
    for path in args.drafts.glob("*.json"):
        draft = json.loads(path.read_text(encoding="utf-8"))
        drafts[draft["conversation"]["conversation_id"]] = draft
    if any(cid not in drafts for cid in ids):
        parser.error("A held-out conversation is missing from the drafts directory")
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
    config = PeftConfig.from_pretrained(args.adapter)
    tokenizer = AutoTokenizer.from_pretrained(args.adapter)
    base = AutoModelForCausalLM.from_pretrained(
        config.base_model_name_or_path, dtype=torch.float16 if use_cuda else torch.float32
    )
    model = PeftModel.from_pretrained(base, args.adapter).to(device)
    model.eval()
    totals: dict[str, int] = defaultdict(int)
    matches: dict[str, int] = defaultdict(int)
    score_errors: list[float] = []
    outcomes = []
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
        try:
            result = EvaluationResult.model_validate_json(extract_json(output))
            validate_result(checklist, conversation, result)
        except (ValueError, ValidationError, InvalidEvaluationResult):
            outcomes.append({"conversation_id": cid, "valid": False})
            continue
        expected = {a.criterion_id: a.value for a in reference.answers}
        for answer in result.answers:
            totals[answer.criterion_id] += 1
            matches[answer.criterion_id] += int(answer.value == expected[answer.criterion_id])
        actual_score = calculate(checklist, result).total
        reference_score = calculate(checklist, reference).total
        if actual_score is not None and reference_score is not None:
            score_errors.append(abs(actual_score - reference_score))
        outcomes.append({"conversation_id": cid, "valid": True})
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
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Valid results: {report['valid']}/{report['selected']}; report: {args.output}")


if __name__ == "__main__":
    main()
