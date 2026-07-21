"""Safe structured logging primitives.

Correlation identifiers live in a ``ContextVar`` so concurrent requests and
LangGraph executions cannot borrow one another's logging context.  Payloads
are deliberately reduced to metadata before JSON serialization.
"""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("ai_native_log_context", default={})
_SENSITIVE_KEY = re.compile(
    r"authorization|token|cookie|raw_payload|prompt|messages|values|series|dto",
    re.IGNORECASE,
)
_STANDARD_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__)


def bind_log_context(**fields: Any) -> Token[dict[str, Any]]:
    """Add correlation fields to the current execution context.

    The returned token must be passed to :func:`clear_log_context` when the
    surrounding request or graph invocation finishes.
    """

    context = dict(_LOG_CONTEXT.get())
    context.update({key: value for key, value in fields.items() if value is not None})
    return _LOG_CONTEXT.set(context)


def clear_log_context(token: Token[dict[str, Any]] | None = None) -> None:
    """Restore a scoped context, or clear all context when no token is given."""

    if token is None:
        _LOG_CONTEXT.set({})
    else:
        _LOG_CONTEXT.reset(token)


def redact(value: Any) -> Any:
    """Recursively remove sensitive request, model, and chart payload fields."""

    if isinstance(value, Mapping):
        return {
            str(key): redact(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return redact(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return redact(model_dump(mode="json"))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return "[unserializable]"


class JsonFormatter(logging.Formatter):
    """Render log records as UTC JSON with redacted context and extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_LOG_CONTEXT.get())
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_LOG_RECORD_KEYS and not key.startswith("_")
            }
        )
        if record.exc_info:
            payload["error"] = record.exc_info[0].__name__
        return json.dumps(redact(payload), ensure_ascii=False, default=str, separators=(",", ":"))
