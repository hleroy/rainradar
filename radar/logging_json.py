"""Structured JSON logging.

A tiny ``logging.Formatter`` subclass that emits one JSON object per line to
stdout — no extra dependency. Event-specific fields are attached via the
standard logging ``extra=`` mechanism and collected here. Use :func:`emit` to
log a canonical archiver/view event so every record carries ``event`` +
``service`` + an ISO-8601 UTC ``timestamp``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging

# LogRecord attributes that are intrinsic to the logging machinery; anything
# else attached via ``extra=`` is treated as an event-specific field.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    },
)


class SuppressCancelledError(logging.Filter):
    """Drop log records whose exception is an :class:`asyncio.CancelledError`.

    Client disconnections (tab close, navigation away) cancel in-flight ASGI
    tasks.  asyncio and asgiref both log those at ERROR, but they are normal
    and harmless — never actionable.  Attach this filter to a handler to
    silence them across all loggers.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            _typ, _val, _tb = record.exc_info
            if _typ is not None and issubclass(_typ, asyncio.CancelledError):
                return False
        return True


class JsonFormatter(logging.Formatter):
    """Render each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Promote event-specific extras (event, service, ts, ...) to top level.
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED and not key.startswith("_")
            },
        )
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def emit(logger: logging.Logger, level: int, event: str, **fields: object) -> None:
    """Log a canonical structured event.

    ``event`` is used both as the human-readable message and as the ``event``
    field; ``service`` defaults to ``"radar"``. Remaining keyword fields become
    top-level JSON keys under the JSON formatter and are silently carried as
    ``extra`` under the plain ``verbose`` formatter.
    """
    extra = {"event": event, "service": fields.pop("service", "radar"), **fields}
    logger.log(level, event, extra=extra)
