"""Optional OpenTelemetry tracing with failure-safe export.

The module deliberately avoids importing OpenTelemetry until tracing is
enabled.  This keeps the default application path independent of an exporter
and makes every manual span safe to leave in business code.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from threading import Lock
from time import monotonic
from typing import Any, Iterator, Mapping

from ai_native.observability.logging import bind_log_context, clear_log_context


logger = logging.getLogger(__name__)

_TRACER: Any | None = None
_PROVIDER: Any | None = None
_HTTPX_INSTRUMENTED = False
_INSTRUMENTED_APPS: set[int] = set()
_CONFIGURATION_LOCK = Lock()
_WARNING_LOCK = Lock()
_LAST_WARNING_AT = 0.0
_WARNING_INTERVAL_SECONDS = 60.0
_SPAN_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "request_id",
        "run_id",
        "conversation_id",
        "user_id",
        "company_id",
        "node",
        "tool",
        "endpoint",
        "status",
        "duration_ms",
        "error",
        "result_count",
        "action_count",
        "observation_count",
        "artifact_count",
        "event_count",
        "operation",
        "resumed",
    }
)


class _NoOpSpan:
    def set_attribute(self, key: str, value: Any) -> None:
        del key, value

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        del attributes


class _SafeSpan:
    """Expose only safe attribute mutation to application code."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        safe = _safe_attributes({key: value})
        if key in safe:
            try:
                self._span.set_attribute(key, safe[key])
            except Exception as exc:
                _warn_tracing_failure(exc)

    def set_attributes(self, attributes: Mapping[str, Any]) -> None:
        for key, value in _safe_attributes(attributes).items():
            try:
                self._span.set_attribute(key, value)
            except Exception as exc:
                _warn_tracing_failure(exc)
                return


class _SafeSpanExporter:
    """Prevent exporter behavior from reaching a request execution path."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    def export(self, spans: Any) -> Any:
        try:
            result = self._delegate.export(spans)
        except Exception as exc:
            _warn_tracing_failure(exc)
            return _failure_export_result()
        if not _export_succeeded(result):
            _warn_tracing_failure(RuntimeError("span_export_failed"))
        return result

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._delegate.shutdown(*args, **kwargs)
        except Exception as exc:
            _warn_tracing_failure(exc)
            return None

    def force_flush(self, *args: Any, **kwargs: Any) -> bool:
        force_flush = getattr(self._delegate, "force_flush", None)
        if not callable(force_flush):
            return True
        try:
            return bool(force_flush(*args, **kwargs))
        except Exception as exc:
            _warn_tracing_failure(exc)
            return False


def configure_tracing(
    app: Any,
    service_name: str,
    *,
    span_exporter: Any | None = None,
) -> Any | None:
    """Configure tracing when ``OTEL_ENABLED`` is true, otherwise do nothing.

    ``span_exporter`` is an injection seam for local validation. Production
    callers use the OTLP/HTTP exporter selected from the standard environment
    variables.
    """

    global _HTTPX_INSTRUMENTED, _PROVIDER, _TRACER

    if not _enabled(os.getenv("OTEL_ENABLED", "false")):
        return None

    with _CONFIGURATION_LOCK:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            if _PROVIDER is None:
                exporter = span_exporter
                if exporter is None:
                    endpoint = os.getenv(
                        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
                    ) or (
                        os.getenv(
                            "OTEL_EXPORTER_OTLP_ENDPOINT",
                            "http://localhost:4318",
                        ).rstrip("/")
                        + "/v1/traces"
                    )
                    exporter = OTLPSpanExporter(
                        endpoint=endpoint,
                        timeout=float(os.getenv("OTEL_EXPORTER_OTLP_TIMEOUT", "5")),
                    )
                provider = TracerProvider(
                    resource=Resource.create({"service.name": str(service_name)[:128]})
                )
                provider.add_span_processor(
                    BatchSpanProcessor(_SafeSpanExporter(exporter))
                )
                _PROVIDER = provider
                _TRACER = provider.get_tracer("ai_native.agent")
                current_provider = trace.get_tracer_provider()
                if current_provider.__class__.__name__ == "ProxyTracerProvider":
                    trace.set_tracer_provider(provider)

            if id(app) not in _INSTRUMENTED_APPS:
                FastAPIInstrumentor.instrument_app(
                    app,
                    tracer_provider=_PROVIDER,
                    excluded_urls=os.getenv(
                        "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
                        "/health/live,/health/ready,/health",
                    ),
                )
                _INSTRUMENTED_APPS.add(id(app))
            if not _HTTPX_INSTRUMENTED:
                HTTPXClientInstrumentor().instrument(tracer_provider=_PROVIDER)
                _HTTPX_INSTRUMENTED = True
            return _PROVIDER
        except Exception as exc:
            _warn_tracing_failure(exc)
            return None


@contextmanager
def agent_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[_SafeSpan | _NoOpSpan]:
    """Start a manual span whose attributes are restricted to safe metadata."""

    tracer = _TRACER
    if tracer is None:
        yield _NoOpSpan()
        return

    selected_name = name if _SPAN_NAME.fullmatch(name) else "agent.operation"
    try:
        scope = tracer.start_as_current_span(
            selected_name,
            attributes=_safe_attributes(attributes or {}),
            record_exception=False,
            set_status_on_exception=False,
        )
        raw_span = scope.__enter__()
    except Exception as exc:
        _warn_tracing_failure(exc)
        yield _NoOpSpan()
        return

    log_token = None
    try:
        correlation = _span_correlation(raw_span)
        if correlation:
            log_token = bind_log_context(**correlation)
        try:
            yield _SafeSpan(raw_span)
        except BaseException as business_error:
            try:
                scope.__exit__(
                    type(business_error), business_error, business_error.__traceback__
                )
            except Exception as exc:
                _warn_tracing_failure(exc)
            raise
        else:
            try:
                scope.__exit__(None, None, None)
            except Exception as exc:
                _warn_tracing_failure(exc)
    finally:
        if log_token is not None:
            clear_log_context(log_token)


def current_trace_context() -> dict[str, str]:
    """Return active OTel identifiers for structured-log correlation."""

    if _TRACER is None:
        return {}
    try:
        from opentelemetry import trace

        return _span_correlation(trace.get_current_span())
    except Exception as exc:
        _warn_tracing_failure(exc)
        return {}


def _safe_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        selected_key = str(key)
        if selected_key not in _SAFE_ATTRIBUTE_KEYS or value is None:
            continue
        if isinstance(value, bool):
            safe[selected_key] = value
        elif isinstance(value, int):
            safe[selected_key] = value
        elif isinstance(value, float):
            safe[selected_key] = round(value, 3)
        elif isinstance(value, str):
            safe[selected_key] = value[:256]
    return safe


def _span_correlation(span: Any) -> dict[str, str]:
    try:
        context = span.get_span_context()
        if not context.is_valid:
            return {}
        return {
            "trace_id": format(context.trace_id, "032x"),
            "span_id": format(context.span_id, "016x"),
        }
    except Exception:
        return {}


def _enabled(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _failure_export_result() -> Any:
    try:
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.FAILURE
    except Exception:
        return None


def _export_succeeded(result: Any) -> bool:
    name = getattr(result, "name", None)
    if isinstance(name, str):
        return name.upper() == "SUCCESS"
    return result == 0


def _warn_tracing_failure(exc: BaseException) -> None:
    global _LAST_WARNING_AT

    now = monotonic()
    with _WARNING_LOCK:
        if now - _LAST_WARNING_AT < _WARNING_INTERVAL_SECONDS:
            return
        _LAST_WARNING_AT = now
    logger.warning(
        "OpenTelemetry tracing degraded",
        extra={"status": "degraded", "error": type(exc).__name__},
    )
