import json
import os
import sys
from copy import deepcopy

import httpx
import pytest

from backend import submission_remote as remote
from backend.submission import SubmissionRequest
from backend.submission_files import publish


@pytest.fixture
def request_and_document():
    request = {
        "split": "prod",
        "team_name": "Synthetic",
        "model_mode": "external_api",
        "model_names": ["synthetic-test-only"],
        "items": [
            {"conversation_id": f"synthetic-{i}", "error": "AI_NOT_CONFIGURED"} for i in range(10)
        ],
    }
    document = {
        "schema_version": "1.0",
        "team_name": "Synthetic",
        "model": {"mode": "external_api", "name": "synthetic-test-only"},
        "results": [
            {**item, "status": "error", "criteria": [], "score": None} for item in request["items"]
        ],
    }
    return request, document


def fetch(request, handler):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return remote.download(
            client,
            remote.endpoint("https://example.test", "dataset-id"),
            SubmissionRequest.model_validate(request),
            timeout=5,
        )


@pytest.mark.parametrize(
    "split,count,batch",
    [("prod", 10, False), ("dev", 65, False), ("prod", 10, True), ("dev", 65, True)],
)
def test_download_selection_and_cli_default_paths(
    monkeypatch, tmp_path, request_and_document, split, count, batch, capsys
):
    request, document = request_and_document
    request["split"] = split
    request["items"] = [
        {"conversation_id": f"synthetic-{i}", "error": "AI_NOT_CONFIGURED"} for i in range(count)
    ]
    document["results"] = [
        {**item, "status": "error", "criteria": [], "score": None} for item in request["items"]
    ]
    if batch:
        del request["items"]
        request["batch_id"] = "batch-id"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(request), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "remote",
            "--api",
            "https://example.test",
            "--dataset-id",
            "dataset-id",
            "--manifest",
            str(manifest),
        ],
    )
    calls = []

    def handler(req):
        calls.append(req)
        assert req.method == "POST"
        assert req.url.path == "/api/v1/datasets/dataset-id/submission"
        assert json.loads(req.content) == SubmissionRequest.model_validate(request).model_dump(
            mode="json"
        )
        return httpx.Response(200, json=document)

    client_type = httpx.Client
    monkeypatch.setattr(
        remote.httpx,
        "Client",
        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw),
    )
    remote.main()
    output = tmp_path / "results" / split / "results.json"
    assert json.loads(output.read_text(encoding="utf-8")) == document
    assert not output.read_bytes().startswith(b"\xef\xbb\xbf")
    assert len(calls) == 1
    assert "OK:" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        remote.main()
    assert exc.value.code == 2 and len(calls) == 1
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--overwrite"])
    remote.main()
    assert len(calls) == 2


@pytest.mark.parametrize("status", [301, 401, 403, 404, 422, 500, 503])
def test_http_errors_never_echo_remote_body(request_and_document, status):
    request, _ = request_and_document
    with pytest.raises(remote.DownloadError, match=f"HTTP {status}") as exc:
        fetch(
            request,
            lambda _: httpx.Response(
                status,
                text="private transcript and secret",
                headers={"location": "https://secret.test"},
            ),
        )
    assert "private" not in str(exc.value) and "secret" not in str(exc.value)


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
def test_network_errors_are_sanitized(request_and_document, error):
    request, _ = request_and_document

    def fail(req):
        raise error("private-token-in-url", request=req)

    with pytest.raises(remote.DownloadError) as exc:
        fetch(request, fail)
    assert "private-token" not in str(exc.value)


@pytest.mark.parametrize(
    "body", [b"{", b'{"secret": 1, "secret": 2}', b'{"secret": NaN}', b"\xff", b"[]"]
)
def test_bad_json(request_and_document, body):
    request, _ = request_and_document
    with pytest.raises(remote.DownloadError, match="INVALID_RESPONSE"):
        fetch(
            request,
            lambda _: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            ),
        )


@pytest.mark.parametrize(
    "change", ["team", "model", "mode", "count", "ids", "duplicate", "outcome", "extra"]
)
def test_mismatching_document(request_and_document, change):
    request, document = request_and_document
    if change == "team":
        document["team_name"] = "Another"
    if change == "model":
        document["model"]["name"] = "Another"
    if change == "mode":
        document["model"]["mode"] = "local"
    if change == "count":
        document["results"].pop()
    if change == "ids":
        document["results"][0]["conversation_id"] = "another"
    if change == "duplicate":
        document["results"][0] = deepcopy(document["results"][1])
    if change == "outcome":
        document["results"][0]["error"] = "Another"
    if change == "extra":
        document["private"] = "unexpected"
    with pytest.raises(remote.DownloadError):
        fetch(request, lambda _: httpx.Response(200, json=document))


@pytest.mark.parametrize("declared", [True, False])
def test_size_limit_even_without_content_length(monkeypatch, request_and_document, declared):
    request, _ = request_and_document
    monkeypatch.setattr(remote, "MAX_JSON_BYTES", 32)
    headers = {"content-type": "application/json"}
    if declared:
        headers["content-length"] = "1000"
    with pytest.raises(remote.DownloadError, match="RESPONSE_TOO_LARGE"):
        fetch(
            request,
            lambda _: httpx.Response(200, stream=httpx.ByteStream(b"x" * 1000), headers=headers),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test",
        "https://example.test/?key=secret",
        "https://example.test/#x",
        "http://example.test",
        "https://example.test/api/v1",
        "file:///data",
    ],
)
def test_invalid_endpoint_does_not_expose_url(url):
    with pytest.raises(remote.DownloadError) as exc:
        remote.endpoint(url, "dataset-id")
    assert "secret" not in str(exc.value)


def test_output_alias_and_atomic_failure(monkeypatch, tmp_path, request_and_document):
    request, _ = request_and_document
    request = SubmissionRequest.model_validate(request)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("input")
    with pytest.raises(remote.DownloadError, match="INVALID_OUTPUT"):
        remote.output_path(request, manifest, manifest, True)
    alias = tmp_path / "alias.json"
    os.link(manifest, alias)
    with pytest.raises(remote.DownloadError, match="INVALID_OUTPUT"):
        remote.output_path(request, manifest, alias, True)
    output = tmp_path / "output.json"
    output.write_text("previous")

    def fail(*args):
        raise PermissionError("synthetic filesystem failure")

    monkeypatch.setattr("backend.submission_files.os.replace", fail)
    with pytest.raises(PermissionError):
        publish(output, "new", True)
    assert output.read_text() == "previous"
    assert {p.name for p in tmp_path.iterdir()} == {"manifest.json", "alias.json", "output.json"}


def test_basic_auth_is_optional_and_partial_credentials_fail(monkeypatch):
    monkeypatch.delenv("CQA_SUBMISSION_HTTP_USER", raising=False)
    monkeypatch.delenv("CQA_SUBMISSION_HTTP_PASSWORD", raising=False)
    assert remote.basic_auth() is None
    monkeypatch.setenv("CQA_SUBMISSION_HTTP_USER", "synthetic")
    with pytest.raises(remote.DownloadError, match="INVALID_AUTH"):
        remote.basic_auth()
    monkeypatch.setenv("CQA_SUBMISSION_HTTP_PASSWORD", "test-only")
    assert isinstance(remote.basic_auth(), httpx.BasicAuth)


@pytest.mark.parametrize("failure", ["http", "json", "timeout", "mismatch"])
def test_cli_failed_download_keeps_existing_file(
    monkeypatch, tmp_path, request_and_document, capsys, failure
):
    request, document = request_and_document
    manifest, output = tmp_path / "manifest.json", tmp_path / "results.json"
    manifest.write_text(json.dumps(request), encoding="utf-8")
    output.write_text("previous", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "remote",
            "--api",
            "https://example.test",
            "--dataset-id",
            "dataset-id",
            "--manifest",
            str(manifest),
            "--output",
            str(output),
            "--overwrite",
        ],
    )
    monkeypatch.setenv("CQA_SUBMISSION_HTTP_USER", "test-user")
    monkeypatch.setenv("CQA_SUBMISSION_HTTP_PASSWORD", "test-secret")
    calls = []

    def handler(req):
        calls.append(req)
        assert req.headers["authorization"].startswith("Basic ")
        if failure == "http":
            return httpx.Response(500, text="test-secret and private data")
        if failure == "json":
            return httpx.Response(
                200, text="test-secret", headers={"content-type": "application/json"}
            )
        if failure == "timeout":
            raise httpx.ReadTimeout("test-secret", request=req)
        document["team_name"] = "Different"
        return httpx.Response(200, json=document)

    client_type = httpx.Client
    monkeypatch.setattr(
        remote.httpx,
        "Client",
        lambda **kw: client_type(transport=httpx.MockTransport(handler), **kw),
    )
    with pytest.raises(SystemExit) as exc:
        remote.main()
    assert exc.value.code == 2
    assert len(calls) == 1
    assert output.read_text() == "previous"
    assert {p.name for p in tmp_path.iterdir()} == {"manifest.json", "results.json"}
    captured = capsys.readouterr()
    assert "test-secret" not in captured.err and "OK:" not in captured.out


def test_download_deadline_between_chunks(monkeypatch, request_and_document):
    request, _ = request_and_document
    clock = iter((0, 6))
    monkeypatch.setattr(remote, "monotonic", lambda: next(clock))
    with pytest.raises(remote.DownloadError, match="DOWNLOAD_TIMEOUT"):
        fetch(
            request,
            lambda _: httpx.Response(
                200, content=b"{}", headers={"content-type": "application/json"}
            ),
        )
