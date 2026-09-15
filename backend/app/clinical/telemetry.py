"""Telemetry seam for OpenTelemetry/Logfire without coupling the graph to an exporter."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from typing import Any, Iterator, Protocol


class RuntimeTelemetry(Protocol):
    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]: ...


class NoopTelemetry:
    """Default implementation: no exporter, no network, no clinical payload capture."""

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        del name, attributes
        yield


@dataclass
class RecordedSpan:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)


class InMemoryTelemetry:
    """Small test double that records span names and metadata only."""

    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        self.spans.append(RecordedSpan(name, dict(attributes or {})))
        yield


_SAFE_ATTRIBUTE_NAMES = frozenset(
    {
        "provider",
        "runtime",
        "node",
        "tool",
        "task_id",
        "hypothesis_id",
        "status",
        "result_count",
        "duration_ms",
        "graph_engine",
    }
)


def _safe_span_attributes(attributes: dict[str, Any] | None) -> dict[str, str | int | float | bool]:
    """Allow only operational metadata; questions and patient rows are intentionally excluded."""

    safe: dict[str, str | int | float | bool] = {}
    for key, value in (attributes or {}).items():
        if key not in _SAFE_ATTRIBUTE_NAMES or not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str) and len(value) > 200:
            safe[key] = value[:200]
        else:
            safe[key] = value
    return safe


class OpenTelemetryRuntimeTelemetry:
    """OpenTelemetry span adapter with a strict metadata allow-list.

    Logfire can consume OpenTelemetry spans in a deployment, but this class never sends clinical
    payloads by itself.  A tracer can be injected in tests or supplied by an application's
    configured OpenTelemetry provider.
    """

    def __init__(self, tracer: Any | None = None, *, instrumentation_name: str = "insightflow.clinical") -> None:
        if tracer is None:
            try:
                from opentelemetry import trace
            except ImportError as exc:  # pragma: no cover - deployment-only path
                raise RuntimeError(
                    "OpenTelemetry runtime is enabled but opentelemetry-api is not installed"
                ) from exc
            tracer = trace.get_tracer(instrumentation_name)
        self._tracer = tracer

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        with self._tracer.start_as_current_span(name) as current_span:
            for key, value in _safe_span_attributes(attributes).items():
                current_span.set_attribute(key, value)
            yield


def build_runtime_telemetry(
    *,
    settings: Any | None = None,
    mode: str | None = None,
) -> RuntimeTelemetry:
    """Select the exporter boundary without making telemetry a clinical dependency."""

    selected = mode
    if selected is None and settings is not None:
        selected = getattr(settings, "insightflow_telemetry", None)
    selected = str(selected or os.getenv("INSIGHTFLOW_TELEMETRY", "noop")).strip().lower()
    if selected in {"noop", "none", "off", "memory"}:
        return NoopTelemetry()
    if selected in {"opentelemetry", "otel", "logfire"}:
        # Logfire can be configured as an OpenTelemetry exporter by the deployment; the runtime
        # only needs the same allow-listed span adapter and does not call an external service here.
        return OpenTelemetryRuntimeTelemetry()
    raise ValueError("unsupported INSIGHTFLOW_TELEMETRY; expected noop, opentelemetry, or logfire")

