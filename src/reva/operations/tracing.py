"""Optional OpenTelemetry tracing (soft dependency).

Spans are created only when the ``opentelemetry`` API is importable **and**
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. If the SDK and the OTLP exporter are
installed too, a tracer provider with a batch OTLP exporter is configured once
(service name from ``OTEL_SERVICE_NAME``, default ``reva-worker``); otherwise
the globally configured provider is used. In every other case :func:`span` is a
no-op context manager, so callers never depend on the package being present.
Attribute values are bounded scalars; no payloads or secrets are attached.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("reva.tracing")

_lock = threading.Lock()
_tracer: Any = None
_resolved = False


def _setup() -> Any:
    global _tracer, _resolved
    with _lock:
        if _resolved:
            return _tracer
        _resolved = True
        if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            return None
        try:
            from opentelemetry import trace
        except ImportError:
            log.info("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry is not installed; tracing disabled")
            return None
        try:  # optional SDK + exporter: configure a provider once
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # type: ignore[import-not-found,unused-ignore]
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found,unused-ignore]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found,unused-ignore]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-not-found,unused-ignore]

            provider = TracerProvider(resource=Resource.create(
                {"service.name": os.environ.get("OTEL_SERVICE_NAME", "reva-worker")}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
        except ImportError:
            log.info("opentelemetry SDK/OTLP exporter not installed; using the global tracer provider")
        except Exception as e:  # never break the worker over telemetry
            log.warning("tracing setup failed: %s", type(e).__name__)
        _tracer = trace.get_tracer("reva")
        return _tracer


def enabled() -> bool:
    return _setup() is not None


def _attr(v: Any) -> Any:
    if isinstance(v, bool | int | float):
        return v
    return str(v)[:256]


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span when tracing is enabled, else a no-op. Yields the span (or ``None``)."""
    tracer = _setup()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as sp:
        for k, v in attributes.items():
            if v is not None:
                sp.set_attribute(f"reva.{k}", _attr(v))
        yield sp


def set_attributes(sp: Any, **attributes: Any) -> None:
    if sp is None:
        return
    for k, v in attributes.items():
        if v is not None:
            sp.set_attribute(f"reva.{k}", _attr(v))


class TracedConnector:
    """Wraps a connector so each fetched item runs inside an ``ingest.item`` span."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._name = inner.describe().name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def fetch(self, item: Any, http: Any) -> Any:
        with span("ingest.item", connector=self._name, item_key=getattr(item, "key", None)) as sp:
            raw = await self._inner.fetch(item, http)
            meta = getattr(raw, "response_metadata", None) or {}
            set_attributes(sp, http_status=meta.get("status"))
            return raw


def traced_connector(connector: Any) -> Any:
    return TracedConnector(connector) if enabled() else connector
