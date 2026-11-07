"""Check export readiness without starting AI, publishing results or executing Git."""

import argparse
import math
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, ValidationError

from .clock import utc_now
from .db import Repository
from .errors import DomainError
from .models import Contract
from .submission import SubmissionDocument, SubmissionRequest, SubmissionService
from .submission_files import matches_json, read_json
from .submission_remote import DownloadError, basic_auth, download, endpoint
from .work_store import WorkStore


class ReadinessReport(Contract):
    report_schema_version: Literal["1.0"] = "1.0"
    checked_at: str = Field(default_factory=utc_now)
    status: Literal["export_ready", "export_ready_with_errors", "blocked"] = "blocked"
    can_export: bool = False
    total_results: int | None = None
    successful_results: int | None = None
    error_results: int | None = None
    failed_conversation_ids: list[str] = Field(default_factory=list)
    file_status: Literal["not_checked", "matches", "mismatch", "unreadable"] = "not_checked"
    issue_code: str | None = None
    message: str
    limitation: str = (
        "Export validation only: AI accuracy, current provider availability/quota, "
        "Git commit/push and the organizer deadline are not checked. "
        "This report is a point-in-time check, not a reservation of batch state."
    )

    @property
    def exit_code(self) -> int:
        return {"export_ready": 0, "export_ready_with_errors": 1, "blocked": 2}[self.status]


def inspect_document(document: SubmissionDocument, results_file: Path | None) -> ReadinessReport:
    """Summarize an already validated export; do not duplicate scoring or selection rules."""
    failed_ids = [row.conversation_id for row in document.results if row.status == "error"]
    report = ReadinessReport(
        status="export_ready_with_errors" if failed_ids else "export_ready",
        can_export=True,
        total_results=len(document.results),
        successful_results=len(document.results) - len(failed_ids),
        error_results=len(failed_ids),
        failed_conversation_ids=failed_ids,
        message=(
            "Export is valid but contains failed dialogues; inspect their outcomes before submission"
            if failed_ids
            else "Selected original AI results can be exported"
        ),
    )
    if results_file is not None:
        try:
            matches = matches_json(read_json(results_file), document.model_dump(mode="json"))
        except (OSError, ValueError, RecursionError):
            return report.model_copy(
                update={
                    "status": "blocked",
                    "file_status": "unreadable",
                    "issue_code": "INVALID_RESULTS_FILE",
                    "message": "Results file is missing, unreadable or invalid strict UTF-8 JSON",
                }
            )
        if not matches:
            return report.model_copy(
                update={
                    "status": "blocked",
                    "file_status": "mismatch",
                    "issue_code": "RESULTS_MISMATCH",
                    "message": "Results file differs from the validated selected original AI results",
                }
            )
        report.file_status = "matches"
    return report


def check(args: argparse.Namespace) -> ReadinessReport:
    try:
        request = SubmissionRequest.model_validate(read_json(args.manifest))
    except (OSError, ValueError, RecursionError):
        return ReadinessReport(
            issue_code="INVALID_MANIFEST",
            message="Manifest is missing, unreadable or invalid; see docs/submission.md",
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        return ReadinessReport(
            issue_code="INVALID_TIMEOUT", message="Timeout must be finite and positive"
        )
    try:
        if args.database is not None:
            if not args.database.is_file():
                return ReadinessReport(
                    issue_code="DATABASE_NOT_FOUND", message="Database file does not exist"
                )
            # No initialize(), schema migration, worker or app import in this CLI.
            repository = Repository(args.database, timeout=args.timeout, read_only=True)
            document = SubmissionService(repository, WorkStore(repository)).generate(
                args.dataset_id, request
            )
        else:
            url = endpoint(args.api, args.dataset_id)
            with httpx.Client(
                auth=basic_auth(),
                follow_redirects=False,
                trust_env=False,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as client:
                # This existing POST only validates/exports stored results; it never evaluates AI.
                document = download(client, url, request, timeout=args.timeout)
        return inspect_document(document, args.results_file)
    except DomainError as exc:
        return ReadinessReport(issue_code=exc.code, message=exc.message)
    except DownloadError as exc:
        # DownloadError contains local diagnostics only, never the remote response body.
        code, _, message = str(exc).partition(": ")
        if code == "HTTP_ERROR":
            message += (
                "; check dataset composition, batch completion and the selected rubric/results"
            )
        return ReadinessReport(issue_code=code, message=message)
    except (ValidationError, ValueError, RecursionError):
        return ReadinessReport(
            issue_code="INVALID_STORED_DATA", message="Stored data could not be validated"
        )
    except OSError:
        return ReadinessReport(issue_code="FILESYSTEM_ERROR", message="Cannot read local files")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--database", type=Path, help="Local SQLite only; read-only mode")
    source.add_argument("--api", help="HTTPS origin; use this for alwaysdata")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--results-file", type=Path, help="Also compare an already exported file")
    parser.add_argument("--timeout", type=float, default=30, help="Read/network timeout in seconds")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")
    args = parser.parse_args()
    report = check(args)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"{report.status.upper()}: {report.message}")
        if report.total_results is not None:
            print(
                f"Results: {report.total_results}; scored: {report.successful_results}; "
                f"errors: {report.error_results}"
            )
        if report.failed_conversation_ids:
            print("Failed conversation IDs: " + ", ".join(report.failed_conversation_ids))
        print(f"Results file: {report.file_status}")
        if report.issue_code:
            print(f"Issue: {report.issue_code}")
        print(report.limitation)
    raise SystemExit(report.exit_code)


if __name__ == "__main__":
    main()
