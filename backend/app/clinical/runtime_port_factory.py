"""Build the V18 replaceable runtime ports from one explicit deployment switch."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from app.clinical.redis_ports import (
    RedisIdempotencyStore,
    RedisRuntimeCache,
    RedisRuntimeEventSink,
    RedisWorkflowCheckpointStore,
    redis_client_from_url,
)
from app.clinical.runtime_ports import (
    InMemoryIdempotencyStore,
    InMemoryRuntimeCache,
    InMemoryRuntimeEventSink,
    InMemoryWorkflowCheckpointStore,
    RuntimeCache,
    RuntimeEventSink,
    RuntimeIdempotency,
    WorkflowCheckpointStore,
)


@dataclass(frozen=True)
class RuntimePortBundle:
    """All non-authoritative runtime services needed by one investigation."""

    mode: str
    cache: RuntimeCache
    checkpoint_store: WorkflowCheckpointStore
    event_sink: RuntimeEventSink
    idempotency: RuntimeIdempotency


def _setting(settings: Any | None, name: str, env_name: str, default: Any) -> Any:
    if settings is not None:
        value = getattr(settings, name, None)
        if value not in {None, ""}:
            return value
    return os.getenv(env_name, default)


def build_runtime_ports(
    *,
    investigation_id: str,
    settings: Any | None = None,
    mode: str | None = None,
    redis_client: Any | None = None,
    namespace: str | None = None,
) -> RuntimePortBundle:
    """Create memory or Redis ports; unknown modes fail closed."""

    selected = str(
        mode
        or _setting(settings, "insightflow_runtime_ports", "INSIGHTFLOW_RUNTIME_PORTS", "memory")
    ).strip().lower()
    if selected in {"memory", "in-memory", "in_memory"}:
        return RuntimePortBundle(
            mode="memory",
            cache=InMemoryRuntimeCache(),
            checkpoint_store=InMemoryWorkflowCheckpointStore(),
            event_sink=InMemoryRuntimeEventSink(),
            idempotency=InMemoryIdempotencyStore(),
        )
    if selected != "redis":
        raise ValueError("unsupported INSIGHTFLOW_RUNTIME_PORTS; expected memory or redis")
    if not investigation_id:
        raise ValueError("investigation_id is required for Redis runtime ports")
    client = redis_client or redis_client_from_url(
        str(_setting(settings, "insightflow_redis_url", "INSIGHTFLOW_REDIS_URL", "redis://localhost:6379/0"))
    )
    prefix = namespace or str(
        _setting(settings, "insightflow_redis_namespace", "INSIGHTFLOW_REDIS_NAMESPACE", "insightflow")
    )
    ttl = int(
        _setting(
            settings,
            "insightflow_runtime_ttl_seconds",
            "INSIGHTFLOW_RUNTIME_TTL_SECONDS",
            3600,
        )
    )
    return RuntimePortBundle(
        mode="redis",
        cache=RedisRuntimeCache(client, namespace=prefix, default_ttl_seconds=ttl),
        checkpoint_store=RedisWorkflowCheckpointStore(client, namespace=prefix, ttl_seconds=ttl),
        event_sink=RedisRuntimeEventSink(client, investigation_id=investigation_id, namespace=prefix),
        idempotency=RedisIdempotencyStore(client, namespace=prefix),
    )

