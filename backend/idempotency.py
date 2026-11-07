"""Canonical request fingerprint shared by evaluation and batch reservations."""

import hashlib
import json

from .models import Contract


def request_fingerprint(request: Contract) -> str:
    return hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
