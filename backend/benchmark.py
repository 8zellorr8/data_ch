"""Read-only CLI: python -m backend.benchmark --help."""

import argparse
from pathlib import Path

from .db import Repository
from .reports import ComparisonManifest, compare_evaluations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare explicitly selected AI/manual evaluations"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("Database does not exist")
    if args.output.resolve() in {args.database.resolve(), args.manifest.resolve()}:
        parser.error("Output must differ from database and manifest")
    manifest = ComparisonManifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    ids = {p.reference_id for p in manifest.pairs} | {p.ai_id for p in manifest.pairs if p.ai_id}
    repository = Repository(args.database)
    records = repository.get_evaluation_selection(ids)
    report = compare_evaluations(manifest, records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(report.purpose)
    print(f"Compared: {report.compared}/{report.selected}; coverage: {report.coverage:.1%}")
    print(
        f"MAE: {report.mae if report.mae is not None else 'unknown'}; "
        f"score pairs: {report.score_compared}; null excluded: {report.score_null_excluded}"
    )
    for metric in report.criteria:
        print(
            f"{metric.checklist_id}@{metric.checklist_version}/{metric.criterion_id}: "
            f"{metric.matches}/{metric.compared}; N/A FP/FN="
            f"{metric.na_false_positive}/{metric.na_false_negative}; flag FP/FN="
            f"{metric.flag_false_positive}/{metric.flag_false_negative}"
        )
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
