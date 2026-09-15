from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Any

SENSITIVE_KEYS = {"api_key", "authorization", "question", "prompt", "rows", "participant_id", "usubjid"}


def redact_metadata(value: dict[str, Any]) -> dict[str, Any]:
    def clean(item):
        if isinstance(item, dict):
            return {key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item
    return clean(value)


class ClinicalOperationsMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests = Counter()
        self._tokens = 0
        self._events = Counter()
        self._duration_ms = 0.0
        self._retries = 0
        self._retry_duration_ms = 0.0
        # Graph/integrity metrics intentionally use a fixed, low-cardinality vocabulary.  They
        # are process-local operational counters; no request text, identifiers, SQL, or result
        # rows are ever accepted as a label.
        self._graph_node_counts = Counter()
        self._graph_node_duration_ms: dict[str, float] = {}
        self._graph_node_failures = Counter()
        self._integrity = Counter()

    @staticmethod
    def _safe_label(value: str, *, fallback: str = "unknown") -> str:
        candidate = str(value).strip().lower()
        if not candidate or len(candidate) > 80 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in candidate):
            return fallback
        return candidate

    def record(self, provider: str, outcome: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self._requests[(provider, outcome)] += 1
            self._tokens += input_tokens + output_tokens

    def record_event(
        self,
        component: str,
        outcome: str,
        duration_ms: float = 0,
        *,
        retry: bool = False,
    ) -> None:
        with self._lock:
            self._events[(component, outcome)] += 1
            elapsed = max(0, duration_ms)
            self._duration_ms += elapsed
            if retry:
                self._retries += 1
                self._retry_duration_ms += elapsed

    def record_graph_node(self, node: str, outcome: str, duration_ms: float = 0) -> None:
        """Record one completed Graph node without allowing high-cardinality labels."""

        safe_node = self._safe_label(node)
        safe_outcome = self._safe_label(outcome)
        elapsed = max(0.0, float(duration_ms))
        with self._lock:
            self._graph_node_counts[(safe_node, safe_outcome)] += 1
            self._graph_node_duration_ms[safe_node] = self._graph_node_duration_ms.get(safe_node, 0.0) + elapsed
            if safe_outcome in {"failure", "failed", "error"}:
                self._graph_node_failures[safe_node] += 1

    def record_integrity(self, category: str, outcome: str = "observed") -> None:
        """Record a bounded audit signal for concurrency, retry, approval, or privacy paths."""

        safe_category = self._safe_label(category)
        if safe_category not in {"cas", "retry", "duplicate", "approval", "privacy"}:
            safe_category = "other"
        safe_outcome = self._safe_label(outcome)
        with self._lock:
            self._integrity[(safe_category, safe_outcome)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests_total": sum(self._requests.values()),
                "tokens_total": self._tokens,
                "by_provider_outcome": {f"{provider}:{outcome}": count for (provider, outcome), count in self._requests.items()},
                "events_total": sum(self._events.values()),
                "duration_ms_total": round(self._duration_ms, 3),
                "retries_total": self._retries,
                "retry_duration_ms_total": round(self._retry_duration_ms, 3),
                "by_component_outcome": {f"{component}:{outcome}": count for (component, outcome), count in self._events.items()},
                "graph_nodes": {
                    "by_node_outcome": {
                        f"{node}:{outcome}": count
                        for (node, outcome), count in self._graph_node_counts.items()
                    },
                    "duration_ms_by_node": {
                        node: round(duration, 3)
                        for node, duration in self._graph_node_duration_ms.items()
                    },
                    "failures_by_node": dict(self._graph_node_failures),
                    "failures_total": sum(self._graph_node_failures.values()),
                },
                "integrity": {
                    "by_category_outcome": {
                        f"{category}:{outcome}": count
                        for (category, outcome), count in self._integrity.items()
                    },
                    "cas_conflicts_total": self._integrity.get(("cas", "conflict"), 0),
                    "retries_total": sum(
                        count for (category, _), count in self._integrity.items() if category == "retry"
                    ),
                    "duplicates_total": sum(
                        count for (category, _), count in self._integrity.items() if category == "duplicate"
                    ),
                    "approvals_total": sum(
                        count for (category, _), count in self._integrity.items() if category == "approval"
                    ),
                    "privacy_events_total": sum(
                        count for (category, _), count in self._integrity.items() if category == "privacy"
                    ),
                },
            }


OPERATIONS = ClinicalOperationsMetrics()

