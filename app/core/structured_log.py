"""JSON logs for the jarvis.* loggers.

Every line carries level, timestamp, correlation_id, mission_id, and the
logger name as `component`. A filter redacts credential-shaped values before
the line is formatted, so a later handler cannot forget.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.correlation import current_correlation_id, current_mission_id
from app.core.redact import redact


class ContextFilter(logging.Filter):
    """Copy the current correlation and mission ids onto the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "correlation_id", ""):
            record.correlation_id = current_correlation_id()  # type: ignore[attr-defined]
        if not getattr(record, "mission_id", ""):
            record.mission_id = current_mission_id()  # type: ignore[attr-defined]
        return True


class RedactionFilter(logging.Filter):
    """Redact secrets in the message and in its interpolation arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        # Arguments first. Redacting the format string itself can eat a `%s`
        # that sits where a secret would, and then `getMessage` raises.
        record.args = _redact_args(record.args)
        if isinstance(record.msg, str) and not record.args:
            record.msg = redact(record.msg)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. `message` is redacted again as a backstop."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "correlation_id": getattr(record, "correlation_id", "") or current_correlation_id(),
            "mission_id": getattr(record, "mission_id", "") or current_mission_id(),
            "component": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Install the JSON handler on the jarvis logger. Safe to call again."""
    logger = logging.getLogger("jarvis")
    logger.setLevel(logging.DEBUG if get_settings().debug else logging.INFO)
    # Keep these lines off the root logger. uvicorn configures that one, and
    # propagating would print every line twice.
    logger.propagate = False
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)


def _redact_args(args: Any) -> Any:
    if isinstance(args, tuple):
        return tuple(redact(item) if isinstance(item, str) else item for item in args)
    if isinstance(args, dict):
        return {key: redact(value) if isinstance(value, str) else value for key, value in args.items()}
    return args
