"""Redact query strings before HTTPX sends signed URLs to log handlers."""

import logging
import re

_QUERY_URL = re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+")


class UrlQueryRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _QUERY_URL.sub(r"\1?[REDACTED]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def configure_logging() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, UrlQueryRedactionFilter) for item in logger.filters):
        logger.addFilter(UrlQueryRedactionFilter())
