"""Download validated organizer results through HTTP; never open the server database."""

import argparse
import math
import os
from pathlib import Path
from time import monotonic

import httpx
from pydantic import TypeAdapter, ValidationError

from .submission import SubmissionDocument, SubmissionRequest
from .submission_files import MAX_JSON_BYTES, parse_json, publish, read_json
from .work_models import SafeId


class DownloadError(Exception):
    """Messages are local constants, never remote bodies or credential-bearing URLs."""


def endpoint(api: str, dataset_id: str) -> httpx.URL:
    try:
        url = httpx.URL(api)
        TypeAdapter(SafeId).validate_python(dataset_id)
    except (httpx.InvalidURL, ValidationError):
        raise DownloadError("INVALID_TARGET: expected a base URL and dataset ID") from None
    if (
        url.scheme not in {"https", "http"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
        or url.path != "/"
        or dataset_id in {".", ".."}
    ):
        raise DownloadError("INVALID_TARGET: use an origin URL without credentials or a path")
    if url.scheme == "http" and url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise DownloadError("HTTPS_REQUIRED: HTTP is allowed only for loopback testing")
    return url.copy_with(path=f"/api/v1/datasets/{dataset_id}/submission")


def basic_auth() -> httpx.BasicAuth | None:
    user = os.getenv("CQA_SUBMISSION_HTTP_USER", "")
    password = os.getenv("CQA_SUBMISSION_HTTP_PASSWORD", "")
    if bool(user) != bool(password):
        raise DownloadError("INVALID_AUTH: set both submission HTTP environment variables")
    return httpx.BasicAuth(user, password) if user else None


def check_selection(document: SubmissionDocument, request: SubmissionRequest) -> None:
    expected_count = 65 if request.split == "dev" else 10
    ids = [row.conversation_id for row in document.results]
    if (
        document.team_name != request.team_name
        or document.model.mode != request.model_mode
        or document.model.name != ", ".join(request.model_names)
        or len(ids) != expected_count
        or len(set(ids)) != len(ids)
        or any(not value.strip() for value in ids)
    ):
        raise DownloadError("RESPONSE_MISMATCH: response does not match submission parameters")
    if request.items:
        selected = {item.conversation_id: item for item in request.items}
        if set(ids) != set(selected):
            raise DownloadError("RESPONSE_MISMATCH: conversation IDs differ from selection")
        for row in document.results:
            item = selected[row.conversation_id]
            if (row.status == "ok") != (item.evaluation_id is not None) or (
                row.status == "error" and row.error != item.error
            ):
                raise DownloadError("RESPONSE_MISMATCH: explicit outcomes differ from selection")
    # JSON 1.0 contains no dataset/batch/evaluation IDs or input snapshots.
    # Their provenance and scoring are checked by SubmissionService on the server.


def download(
    client: httpx.Client,
    url: httpx.URL,
    request: SubmissionRequest,
    *,
    timeout: float,
) -> SubmissionDocument:
    deadline = monotonic() + timeout
    try:
        with client.stream(
            "POST", url, json=request.model_dump(mode="json"), timeout=timeout
        ) as response:
            if response.status_code != 200:
                raise DownloadError(f"HTTP_ERROR: server returned HTTP {response.status_code}")
            media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if media_type != "application/json":
                raise DownloadError("INVALID_RESPONSE: expected application/json")
            length = response.headers.get("content-length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise DownloadError("INVALID_RESPONSE: invalid Content-Length")
                if int(length) > MAX_JSON_BYTES:
                    raise DownloadError("RESPONSE_TOO_LARGE: maximum is 16 MiB")
            raw = bytearray()
            for chunk in response.iter_bytes():
                if monotonic() > deadline:
                    raise DownloadError("DOWNLOAD_TIMEOUT: response deadline exceeded")
                if len(raw) + len(chunk) > MAX_JSON_BYTES:
                    raise DownloadError("RESPONSE_TOO_LARGE: maximum is 16 MiB")
                raw.extend(chunk)
        document = SubmissionDocument.model_validate(parse_json(bytes(raw)), strict=True)
    except httpx.TimeoutException:
        raise DownloadError("DOWNLOAD_TIMEOUT: request timed out") from None
    except httpx.HTTPError:
        raise DownloadError("NETWORK_ERROR: could not read the submission response") from None
    except (ValueError, RecursionError):
        raise DownloadError("INVALID_RESPONSE: invalid submission JSON") from None
    check_selection(document, request)
    return document


def output_path(
    request: SubmissionRequest, manifest: Path, output: Path | None, overwrite: bool
) -> Path:
    path = output or Path("results") / request.split / "results.json"
    if path.is_symlink() or path.resolve() == manifest.resolve():
        raise DownloadError("INVALID_OUTPUT: output must not alias the manifest or be a symlink")
    if path.exists():
        if not path.is_file() or os.path.samefile(path, manifest):
            raise DownloadError("INVALID_OUTPUT: destination must be a separate regular file")
        if not overwrite:
            raise DownloadError("OUTPUT_EXISTS: use --overwrite to replace results")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True, help="HTTPS origin; no /api/v1 suffix")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Default: results/<split>/results.json")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--timeout", type=float, default=30, help="Network timeout in seconds")
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise DownloadError("INVALID_TIMEOUT: use a finite positive number")
        url = endpoint(args.api, args.dataset_id)
        request = SubmissionRequest.model_validate(read_json(args.manifest))
        output = output_path(request, args.manifest, args.output, args.overwrite)
        with httpx.Client(
            auth=basic_auth(),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as client:
            document = download(client, url, request, timeout=args.timeout)
        publish(output, document.model_dump_json(indent=2), args.overwrite)
    except DownloadError as exc:
        parser.exit(2, f"{exc}\n")
    except (ValueError, RecursionError):
        parser.exit(2, "INVALID_MANIFEST: see docs/submission.md\n")
    except OSError as exc:
        parser.exit(2, f"FILESYSTEM_ERROR: {type(exc).__name__}\n")
    errors = sum(row.status == "error" for row in document.results)
    print(f"OK: {len(document.results)} results, {errors} errors; saved {output}")


if __name__ == "__main__":
    main()
