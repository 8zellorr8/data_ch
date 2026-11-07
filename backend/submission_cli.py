"""Export or recheck submission files without starting the API, worker or AI."""

import argparse
import os
from pathlib import Path

from pydantic import ValidationError

from .db import Repository
from .errors import DomainError
from .submission import SubmissionRequest, SubmissionService
from .submission_files import matches_json, publish, read_json
from .work_store import WorkStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Default: results/<split>/results.json")
    parser.add_argument("--check", action="store_true", help="Check existing file; write nothing")
    parser.add_argument("--overwrite", action="store_true", help="Replace an earlier results file")
    args = parser.parse_args()
    try:
        if not args.database.is_file():
            raise ValueError("Database does not exist")
        request = SubmissionRequest.model_validate(read_json(args.manifest))
        output = args.output or Path("results") / request.split / "results.json"
        protected = {args.database.resolve(), args.manifest.resolve()}
        protected.update(Path(str(args.database) + suffix).resolve() for suffix in ("-wal", "-shm"))
        if output.is_symlink() or output.resolve() in protected:
            raise ValueError("Output must not be a symlink, database, sidecar or input manifest")
        if output.exists() and not args.check:
            if not args.overwrite:
                raise ValueError("Output exists; use --check or explicitly --overwrite")
            if any(os.path.samefile(output, source) for source in (args.database, args.manifest)):
                raise ValueError("Output aliases an input file")
        repository = Repository(args.database)
        document = SubmissionService(repository, WorkStore(repository)).generate(
            args.dataset_id, request
        )
        if args.check:
            if not matches_json(read_json(output), document.model_dump(mode="json")):
                raise ValueError("File differs from validated original AI results")
        else:
            publish(output, document.model_dump_json(indent=2), args.overwrite)
    except DomainError as exc:
        parser.exit(2, f"{exc.code}: {exc.message}\n")
    except ValidationError:
        parser.exit(2, "Invalid submission manifest; see docs/submission.md\n")
    except (OSError, ValueError) as exc:
        # Never print file contents or provider responses in CLI diagnostics.
        message = (
            str(exc)
            if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError)
            else type(exc).__name__
        )
        parser.exit(2, f"Submission failed: {message}\n")
    errors = sum(result.status == "error" for result in document.results)
    print(
        f"OK: {len(document.results)} results, {errors} errors; {'checked' if args.check else 'saved'} {output}"
    )


if __name__ == "__main__":
    main()
