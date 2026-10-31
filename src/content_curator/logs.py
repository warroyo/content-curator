"""Structured JSON logs on stderr."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update({k: v for k, v in vars(record).items() if k not in _STANDARD})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "trafilatura", "htmldate", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
