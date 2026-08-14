"""Structured JSON log formatter + emit helper."""

from __future__ import annotations

import json
import logging

from radar.logging_json import JsonFormatter
from radar.logging_json import emit


def _record(**extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="radar.archiver",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="poll_complete",
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_formatter_emits_single_line_json():
    rec = _record(event="poll_complete", service="radar", tiles_written=12)
    line = JsonFormatter().format(rec)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["event"] == "poll_complete"
    assert payload["service"] == "radar"
    assert payload["tiles_written"] == 12
    assert payload["level"] == "INFO"
    assert "timestamp" in payload


def test_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys  # noqa: PLC0415

        rec = logging.LogRecord(
            "radar",
            logging.ERROR,
            __file__,
            1,
            "fail",
            (),
            sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(rec))
    assert "ValueError" in payload["exc_info"]


def test_emit_attaches_event_fields(caplog):
    logger = logging.getLogger("radar.test")
    with caplog.at_level(logging.INFO, logger="radar.test"):
        emit(logger, logging.INFO, "frame_archived", ts=123, status="ok")
    rec = caplog.records[-1]
    assert rec.event == "frame_archived"
    assert rec.service == "radar"
    assert rec.ts == 123
    assert rec.status == "ok"
