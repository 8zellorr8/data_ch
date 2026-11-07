"""Compare saved provider assessments with local references without more API calls."""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from backend.models import ChecklistVersion, Conversation, EvaluationResult
from backend.scoring import calculate, validate_result


def compare(references: list[Path], predictions: Path) -> dict[str, object]:
    refs = {}
    for directory in references:
        for path in sorted(directory.glob("*.json")):
            draft = json.loads(path.read_text(encoding="utf-8"))
            refs[draft["conversation"]["conversation_id"]] = draft
    compared: Counter[str] = Counter()
    matches: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    statuses: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    channels: Counter[str] = Counter()
    models: Counter[str] = Counter()
    prompt_versions: Counter[str] = Counter()
    thinking_levels: Counter[str] = Counter()
    language_matches: Counter[str] = Counter()
    language_compared: Counter[str] = Counter()
    language_errors: dict[str, list[float]] = defaultdict(list)
    na_counts: Counter[str] = Counter()
    errors = []
    durations = []
    outcomes = []
    assessed = set()
    calls = 0
    valid = 0
    for path in sorted(predictions.glob("*.json")):
        prediction = json.loads(path.read_text(encoding="utf-8"))
        conversation = Conversation.model_validate(prediction["conversation"])
        checklist = ChecklistVersion.model_validate(prediction["checklist"])
        result = EvaluationResult.model_validate(prediction["result"])
        validate_result(checklist, conversation, result)
        valid += 1
        cid = conversation.conversation_id
        if cid in assessed:
            raise ValueError(f"Duplicate saved conversation ID: {cid}")
        assessed.add(cid)
        languages[conversation.language] += 1
        channels[conversation.channel] += 1
        models[prediction.get("teacher_model", "unknown")] += 1
        prompt_versions[prediction.get("prompt_version", "unknown")] += 1
        thinking_levels[prediction.get("teacher_thinking_level", "unknown")] += 1
        calls += prediction.get("calls", 1)
        if "seconds" in prediction:
            durations.append(prediction["seconds"])
        actual_score = calculate(checklist, result).total
        outcome = {
            "conversation_id": cid,
            "channel": conversation.channel,
            "language": conversation.language,
            "reference_available": cid in refs,
            "predicted": {a.criterion_id: a.value for a in result.answers},
            "predicted_score": actual_score,
        }
        if cid in refs:
            reference_draft = refs[cid]
            ref_conversation = Conversation.model_validate(reference_draft["conversation"])
            ref_checklist = ChecklistVersion.model_validate(reference_draft["checklist"])
            if conversation != ref_conversation or checklist != ref_checklist:
                raise ValueError(f"Reference inputs differ for {cid}")
            reference = EvaluationResult.model_validate(reference_draft["result"])
            validate_result(checklist, conversation, reference)
            expected = {a.criterion_id: a.value for a in reference.answers}
            for answer in result.answers:
                compared[answer.criterion_id] += 1
                matched = int(answer.value == expected[answer.criterion_id])
                matches[answer.criterion_id] += matched
                language_compared[conversation.language] += 1
                language_matches[conversation.language] += matched
                na_counts["reference"] += int(expected[answer.criterion_id] == "na")
                na_counts["predicted"] += int(answer.value == "na")
                na_counts["matched"] += int(
                    answer.value == "na" and expected[answer.criterion_id] == "na"
                )
                confusion[answer.criterion_id][
                    f"{expected[answer.criterion_id]} -> {answer.value}"
                ] += 1
            statuses[reference_draft.get("annotation_status", "unknown")] += 1
            reference_score = calculate(checklist, reference).total
            if actual_score is not None and reference_score is not None:
                errors.append(abs(actual_score - reference_score))
                language_errors[conversation.language].append(abs(actual_score - reference_score))
            outcome.update(
                reference=expected, predicted_score=actual_score, reference_score=reference_score
            )
        outcomes.append(outcome)
    return {
        "saved_valid_assessments": valid,
        "known_successful_request_calls": calls,
        "models": dict(models),
        "prompt_versions": dict(prompt_versions),
        "thinking_levels": dict(thinking_levels),
        "languages": dict(languages),
        "channels": dict(channels),
        "reference_status": dict(statuses),
        "missing_reference_predictions": sorted(set(refs) - assessed),
        "criterion_accuracy": {
            cid: {"matches": matches[cid], "compared": count} for cid, count in compared.items()
        },
        "criterion_confusion": {cid: dict(counts) for cid, counts in confusion.items()},
        "score_mae": statistics.mean(errors) if errors else None,
        "language_quality": {
            language: {
                "answer_matches": language_matches[language],
                "answers_compared": count,
                "conversations_compared": len(language_errors[language]),
                "score_mae": statistics.mean(language_errors[language])
                if language_errors[language]
                else None,
            }
            for language, count in language_compared.items()
        },
        "na_comparison": dict(na_counts),
        "median_seconds": statistics.median(durations) if durations else None,
        "max_seconds": max(durations) if durations else None,
        "mean_seconds": statistics.mean(durations) if durations else None,
        "outcomes": outcomes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--references",
        nargs="+",
        type=Path,
        default=[Path("data/labels/expanded-drafts"), Path("data/labels/voice-draft")],
    )
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/labels/provider-comparison.json"))
    args = parser.parse_args()
    report = compare(args.references, args.predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Valid saved assessments: {report['saved_valid_assessments']}; MAE: {report['score_mae']}; {args.output}"
    )


if __name__ == "__main__":
    main()
