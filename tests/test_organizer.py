import asyncio
import json
import logging

import httpx
import pytest

from backend.errors import DomainError
from backend.models import OrganizerImport
from backend.organizer import fetch_dataset


@pytest.mark.parametrize(
    "kind", ["valid", "array", "invalid_schema", "failed", "redirect", "unavailable"]
)
def test_organizer_contract_and_redacted_errors(monkeypatch, dataset, kind, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    original_client = httpx.AsyncClient

    def handler(request):
        assert request.url.params["group_id"] == "CONTROL"
        assert request.url.params["sig"] == "private-test-secret"
        if kind == "unavailable":
            raise httpx.ConnectError("private-test-secret", request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://localhost/private"})
        payload = {"isSuccess": True, "data": {"records": dataset["records"]}}
        if kind == "array":
            payload = []
        elif kind == "invalid_schema":
            payload["data"]["records"][0]["language"] = "XX"
        elif kind == "failed":
            payload["isSuccess"] = False
        return httpx.Response(
            200,
            content=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json; charset=latin-1"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    coroutine = fetch_dataset(
        "https://test.environment.api.powerplatform.com/invoke?sig=private-test-secret&group_id=DEV",
        OrganizerImport(group_id="CONTROL"),
    )
    if kind == "valid":
        result = asyncio.run(coroutine)
        assert result.records[0].transcript[0].text == "Здравствуйте! Где инструкция?"
        assert result.group_id == "CONTROL"
    else:
        with pytest.raises(DomainError) as error:
            asyncio.run(coroutine)
        assert error.value.status == 502
        assert "private-test-secret" not in error.value.message
    assert "private-test-secret" not in caplog.text


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "https://example.com",
        "https://evil.environment.api.powerplatform.com.evil.com",
    ],
)
def test_wrong_organizer_host_rejected(url):
    with pytest.raises(DomainError) as error:
        asyncio.run(fetch_dataset(url, OrganizerImport()))
    assert error.value.code == "ORGANIZER_CONFIG_ERROR"
