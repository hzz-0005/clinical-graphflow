"""Replaceable runtime ports for checkpoints, cache and graph events.

The first implementation is intentionally in-memory.  It makes the ownership boundary explicit:
PostgreSQL remains the authoritative clinical fact/evidence store, while these ports are only for
replay, idempotency and observability.  Redis and Temporal can implement the same protocols later
without changing a clinical tool or the investigation graph.
"""

from __future__ import annotations

from copy import deepcopy
import threading
from typing import Any, Protocol

from app.clinical.v17_contracts import GraphEvent, InvestigationGraphState


class RuntimeEventSink(Protocol):
    """Append-only sink for graph events; it must not mutate clinical facts."""

    def emit(self, event: GraphEvent) -> None: ...


class RuntimeCache(Protocol):
    """Non-authoritative cache contract.

    Implementations must expose ``is_final_fact_store=False`` so callers cannot mistake a cache
    hit for a published clinical result.
    """

    is_final_fact_store: bool

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...


class WorkflowCheckpointStore(Protocol):
    """Checkpoint contract for pause/resume; checkpoints are not evidence publication."""

    is_final_fact_store: bool

    def save(self, state: InvestigationGraphState) -> None: ...

    def load(self, investigation_id: str) -> InvestigationGraphState | None: ...


class RuntimeIdempotency(Protocol):
    """Short-lived claim used to deduplicate a request; it never stores a result."""

    def try_claim(self, key: str, *, owner: str, ttl_seconds: int = 300) -> bool: ...

    def owner(self, key: str) -> str | None: ...

    def release(self, key: str, *, owner: str) -> None: ...


class InMemoryRuntimeCache:
    is_final_fact_store = False

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        value = self._values.get(key)
        return deepcopy(value)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        # TTL is accepted now so a Redis implementation can be substituted later.  The in-memory
        # test port deliberately does not make wall-clock expiry part of deterministic tests.
        del ttl_seconds
        self._values[key] = deepcopy(value)


class InMemoryRuntimeEventSink:
    """Deterministic event sink used by tests and local development."""

    def __init__(self) -> None:
        self._events: list[GraphEvent] = []

    def emit(self, event: GraphEvent) -> None:
        self._events.append(event.model_copy(deep=True))

    def list_events(self) -> tuple[GraphEvent, ...]:
        return tuple(item.model_copy(deep=True) for item in self._events)


class InMemoryWorkflowCheckpointStore:
    is_final_fact_store = False

    def __init__(self) -> None:
        self._payloads: dict[str, str] = {}
        self._lock = threading.RLock()

    def save(self, state: InvestigationGraphState) -> None:
        state.assert_internal_version_consistency()
        payload = state.model_dump_json()
        with self._lock:
            previous = self._payloads.get(state.investigation_id)
            if previous is not None:
                current = InvestigationGraphState.from_canonical_snapshot(previous)
                if state.checkpoint_version < current.checkpoint_version:
                    raise ValueError("stale checkpoint version")
                if state.checkpoint_version == current.checkpoint_version and payload != previous:
                    raise ValueError("checkpoint version conflict")
            self._payloads[state.investigation_id] = payload

    def load(self, investigation_id: str) -> InvestigationGraphState | None:
        with self._lock:
            payload = self._payloads.get(investigation_id)
        if payload is None:
            return None
        return InvestigationGraphState.from_canonical_snapshot(payload)


class InMemoryIdempotencyStore:
    """Deterministic request-claim port for local development and tests."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    def try_claim(self, key: str, *, owner: str, ttl_seconds: int = 300) -> bool:
        del ttl_seconds
        if not key or not owner:
            raise ValueError("idempotency key and owner cannot be empty")
        if key in self._owners:
            return False
        self._owners[key] = owner
        return True

    def owner(self, key: str) -> str | None:
        return self._owners.get(key)

    def release(self, key: str, *, owner: str) -> None:
        if self._owners.get(key) == owner:
            self._owners.pop(key, None)

