"""Structured logging.

Logging philosophy for a batch workload
--------------------------------------
A daily sweep issues 100,000 requests. Logging one line per request produces 100,000
lines that nobody reads and that cost real money to store and index. Logging nothing
means a 3% failure rate is invisible until it shows up as missing data downstream.

So: **metrics for the aggregate, logs for the exceptional.**

* Successful requests are never logged individually. Their existence, latency, cost
  and token counts are already in Prometheus, which is the right tool for "how many"
  and "how fast".
* Every failure is logged once, with enough metadata to diagnose it without a repro:
  the error class, the vendor status code, which attempt it was, how long it took,
  what was retried, and what the vendor said.
* Truncated and blocked answers are logged too. They arrive as HTTP 200 and would
  otherwise be the quietest failure mode in the system.

Output is JSON on one line, because these logs are meant to be queried rather than
read. ``LOG_FORMAT=text`` switches to a human-readable form for local work.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with arbitrary structured context.

    Anything attached to the record via ``extra={...}`` is merged into the top level,
    so a caller can add error-specific fields without a bespoke formatter per error.
    """

    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Install the root handler. Safe to call more than once."""
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.getenv("LOG_FORMAT", "json")).lower()

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level)

    # The SDK logs an automatic-function-calling advisory on every call. We pass no
    # tools, so it is pure noise that would drown real errors at batch volume.
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)
    for noisy in ("httpx", "httpcore", "urllib3", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_failure(
    logger: logging.Logger,
    message: str,
    *,
    error: Exception | None = None,
    **context: Any,
) -> None:
    """Log a failure once, with everything needed to diagnose it.

    Deliberately verbose in metadata and sparse in frequency: failures are rare
    enough that a fat record is cheap, and a thin one usually means going back to
    reproduce something that has already happened.
    """
    payload: dict[str, Any] = {k: v for k, v in context.items() if v is not None}
    if error is not None:
        payload["error_class"] = type(error).__name__
        payload["error_message"] = str(error)[:500]
        for attr in ("status_code", "retry_after_s", "provider"):
            value = getattr(error, attr, None)
            if value is not None:
                payload[attr] = value
    logger.error(message, extra=payload)
