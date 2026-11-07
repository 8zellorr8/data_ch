import asyncio
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from .errors import DomainError
from .models import DatasetImport, OrganizerImport

MAX_BYTES = 8 * 1024 * 1024


async def fetch_dataset(api_url: str, request: OrganizerImport) -> DatasetImport:
    if not api_url:
        raise DomainError(
            "ORGANIZER_NOT_CONFIGURED",
            "Set CQA_ORGANIZER_API_URL on the server or import a JSON body",
        )
    parsed = urlsplit(api_url)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".environment.api.powerplatform.com")
        or parsed.username
        or parsed.password
    ):
        raise DomainError(
            "ORGANIZER_CONFIG_ERROR", "Organizer URL must be an HTTPS Power Platform endpoint"
        )
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "group_id"]
    query.append(("group_id", request.group_id))
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    try:
        async with (
            asyncio.timeout(45),
            httpx.AsyncClient(timeout=45, follow_redirects=False) as client,
        ):
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise DomainError(
                        "ORGANIZER_UNAVAILABLE", f"Organizer returned HTTP {response.status_code}"
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise DomainError("DATASET_TOO_LARGE", "Organizer dataset exceeds 8 MiB")
                    chunks.append(chunk)
        # Ignore a mistaken charset header: the organizers' JSON is UTF-8.
        payload = json.loads(b"".join(chunks).decode("utf-8-sig"))
        if (
            not isinstance(payload, dict)
            or payload.get("isSuccess") is not True
            or not isinstance(payload.get("data"), dict)
        ):
            raise DomainError(
                "ORGANIZER_INVALID_RESPONSE", "Organizer did not return a successful dataset"
            )
        return DatasetImport(
            name=request.name, group_id=request.group_id, records=payload["data"]["records"]
        )
    except (httpx.RequestError, TimeoutError):
        # Do not expose signed URLs, authorization values or remote response bodies.
        raise DomainError(
            "ORGANIZER_UNAVAILABLE", "Unable to fetch the organizer dataset"
        ) from None
    except (ValueError, KeyError, TypeError, ValidationError):
        raise DomainError(
            "ORGANIZER_INVALID_RESPONSE",
            "Organizer returned invalid UTF-8 JSON or conversation schema",
        ) from None


class OrganizerClient:
    def __init__(self, api_url: str) -> None:
        self._api_url = api_url

    async def fetch(self, request: OrganizerImport) -> DatasetImport:
        return await fetch_dataset(self._api_url, request)
