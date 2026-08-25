"""Structured logging, on structlog.

`structlog` rather than a hand-rolled JSON formatter because error-tracking
integration is what decides it. Sentry, Datadog and OpenTelemetry all ship structlog
processors, so adding one is a line in the processor chain rather than a rewrite. A
bespoke `logging.Formatter` would have to grow that integration by hand, and exception
grouping and context propagation are not worth reimplementing.

Logging policy for a batch workload
-----------------------------------
A daily sweep issues 100,000 requests. One line each is 100,000 lines nobody reads and
that cost money to store. Logging nothing makes a 3% failure rate invisible until it
surfaces as missing data downstream.

So: metrics for the aggregate, logs for the exceptional. Successful requests are never
logged individually - their latency, cost and token counts are in Prometheus, which is
the right tool for "how many" and "how fast". Failures are logged once with enough
context to diagnose without a repro. Truncated and blocked answers are logged too;
they arrive as HTTP 200 and are otherwise the quietest failure mode in the system.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_configured = False


def _rename_keys(_logger, _name, event_dict):
    """Match the field names the dashboards and committed evidence already parse."""
    if "event" in event_dict:
        event_dict["msg"] = event_dict.pop("event")
    if "timestamp" in event_dict:
        event_dict["ts"] = event_dict.pop("timestamp")
    return event_dict


def configure(level: str | None = None, fmt: str | None = None) -> None:
    """Install processors and route stdlib logging through them. Idempotent."""
    global _configured
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    fmt = (fmt or os.getenv("LOG_FORMAT", "json")).lower()

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _rename_keys,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(default=str)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    # foreign_pre_chain covers libraries logging through stdlib directly, so an httpx
    # warning comes out in the same shape as ours.
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(processor=renderer, foreign_pre_chain=shared)
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_name)

    # The SDK emits an automatic-function-calling advisory on every call. At batch
    # volume that noise buries real errors.
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)
    for noisy in ("httpx", "httpcore", "urllib3", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> Any:
    if not _configured:
        configure()
    return structlog.stdlib.get_logger(name)


def log_failure(
    logger: Any, message: str, *, error: Exception | None = None, **context: Any
) -> None:
    """Log a failure once, with everything needed to diagnose it without a repro.

    Verbose in metadata and sparse in frequency: failures are rare enough that a fat
    record is cheap, while a thin one usually means reproducing something that has
    already happened.

    Pulls `status_code` and `retry_after_s` off the exception when present, since those
    two decide whether a failure was ours or the vendor's.
    """
    payload = {k: v for k, v in context.items() if v is not None}
    if error is not None:
        payload["error_class"] = type(error).__name__
        payload["error_message"] = str(error)[:500]
        for attr in ("status_code", "retry_after_s", "provider"):
            value = getattr(error, attr, None)
            if value is not None:
                payload[attr] = value
    logger.error(message, **payload)
