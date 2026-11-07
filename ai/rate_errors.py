"""Extract bounded scheduling hints without exposing provider messages or secrets."""

import math
from email.utils import parsedate_to_datetime

import httpx

MAX_DELAY = 366 * 24 * 60 * 60


def duration(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 <= number <= MAX_DELAY else None


def retry_hint(response: httpx.Response, now: float) -> tuple[float, bool]:
    delays: list[float] = []
    header = response.headers.get("retry-after", "")
    seconds = duration(header)
    if seconds is None and header:
        try:
            seconds = duration(max(0, parsedate_to_datetime(header).timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            pass
    if seconds is not None:
        delays.append(seconds)
    quota = False
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details", []) if isinstance(error, dict) else []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
            raw = detail.get("retryDelay")
            seconds = duration(raw[:-1]) if isinstance(raw, str) and raw.endswith("s") else None
            if seconds is not None:
                delays.append(seconds)
        if detail.get("@type") == "type.googleapis.com/google.rpc.QuotaFailure":
            violations = detail.get("violations", [])
            for violation in violations if isinstance(violations, list) else []:
                if not isinstance(violation, dict):
                    continue
                identifier = str(violation.get("quotaId", "")).lower()
                quota |= "perday" in identifier or "per_day" in identifier
    # Missing hints must not cause a tight retry loop or guessed daily reset.
    delay = max(delays) if delays else (86400 if quota else 60)
    return now + max(delay, 1), quota
