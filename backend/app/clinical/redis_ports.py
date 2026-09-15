"""Optional Redis implementations for the V18 runtime ports.

Redis is deliberately used only for short-lived coordination data: cached metadata, graph
checkpoints, append-only runtime events, and idempotency claims.  PostgreSQL remains the source of
truth for published clinical facts, evidence, approvals, and audit records.

The module does not import ``redis`` at module import time.  This keeps the default in-memory
runtime and unit tests dependency-free; production can install the ``durable`` extra and pass a
real redis-py client (or use :func:`redis_client_from_url`).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from app.clinical.runtime_ports import RuntimeCache, RuntimeEventSink, WorkflowCheckpointStore
from app.clinical.v17_contracts import GraphEvent, InvestigationGraphState


class RedisCommandClient(Protocol):
    """Small synchronous redis-py surface required by the adapters."""

    def get(self, key: str) -> bytes | str | None: ...

    def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None: ...

    def rpush(self, key: str, value: str) -> int: ...

    def lrange(self, key: str, start: int, end: int) -> list[bytes | str]: ...

    def delete(self, *keys: str) -> int: ...


class RedisPortError(RuntimeError):
    """Raised when Redis data cannot be decoded into a typed runtime object."""


_CHECKPOINT_SAVE_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current then
  local ok, decoded = pcall(cjson.decode, current)
  if not ok or type(decoded) ~= 'table' or decoded['checkpoint_version'] == nil then
    return -3
  end
  local current_version = tonumber(decoded['checkpoint_version'])
  local incoming_version = tonumber(ARGV[2])
  if not current_version or not incoming_version then
    return -3
  end
  if incoming_version < current_version then
    return -1
  end
  if incoming_version == current_version then
    if current == ARGV[1] then
      return 0
    end
    return -2
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
return 1
"""


def _json_value(value: bytes | str) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _key(namespace: str, kind: str, identifier: str) -> str:
    namespace = namespace.strip(": ") or "insightflow"
    if not identifier:
        raise ValueError("Redis runtime key identifier cannot be empty")
    return f"{namespace}:{kind}:{identifier}"


def redis_client_from_url(url: str) -> RedisCommandClient:
    """Construct a redis-py client only when the durable extra is explicitly enabled."""

    try:
        import redis
    except ImportError as exc:  # pragma: no cover - exercised by deployment smoke tests
        raise RedisPortError(
            "Redis runtime is enabled but redis-py is not installed; install insightflow-backend[durable]"
        ) from exc
    return redis.Redis.from_url(url, decode_responses=False)


class RedisRuntimeCache(RuntimeCache):
    """JSON cache with an explicit non-authoritative fact-store marker."""

    is_final_fact_store = False

    def __init__(
        self,
        client: RedisCommandClient,
        *,
        namespace: str = "insightflow",
        default_ttl_seconds: int = 300,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        self._client = client
        self._namespace = namespace
        self._default_ttl_seconds = default_ttl_seconds

    def get(self, key: str) -> Any | None:
        payload = self._client.get(_key(self._namespace, "cache", key))
        if payload is None:
            return None
        try:
            return json.loads(_json_value(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPortError("Redis cache entry is not valid JSON") from exc

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RedisPortError("Redis cache values must be JSON serializable") from exc
        self._client.set(_key(self._namespace, "cache", key), payload, ex=ttl)


class RedisWorkflowCheckpointStore(WorkflowCheckpointStore):
    """Typed checkpoint storage; checkpoint data is never treated as clinical evidence."""

    is_final_fact_store = False

    def __init__(
        self,
        client: RedisCommandClient,
        *,
        namespace: str = "insightflow",
        ttl_seconds: int = 3_600,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._client = client
        self._namespace = namespace
        self._ttl_seconds = ttl_seconds

    def save(self, state: InvestigationGraphState) -> None:
        state.assert_internal_version_consistency()
        key = _key(self._namespace, "checkpoint", state.investigation_id)
        payload = state.model_dump_json()
        evaluator = getattr(self._client, "eval", None)
        if callable(evaluator):
            try:
                result = evaluator(
                    _CHECKPOINT_SAVE_SCRIPT,
                    1,
                    key,
                    payload,
                    str(state.checkpoint_version),
                    str(self._ttl_seconds),
                )
            except (AttributeError, NotImplementedError):
                # A narrow compatibility fallback is retained for lightweight clients and test
                # doubles.  redis-py itself always exposes EVAL, so production deployments use
                # the atomic Lua branch above.
                result = None
            else:
                self._assert_save_result(result)
                return
            if result is not None:
                return

        # Compatibility fallback for clients without EVAL.  If the client exposes WATCH/MULTI,
        # use its optimistic transaction so concurrent writers cannot silently overwrite a newer
        # checkpoint.  The final get/set branch is intentionally limited to tiny test doubles.
        pipeline_factory = getattr(self._client, "pipeline", None)
        if callable(pipeline_factory):
            if self._save_with_pipeline(pipeline_factory, key, payload, state):
                return
        current_payload = self._client.get(key)
        self._assert_compatible_current(current_payload, state, payload)
        self._client.set(key, payload, ex=self._ttl_seconds)

    @staticmethod
    def _assert_save_result(result: Any) -> None:
        try:
            code = int(result)
        except (TypeError, ValueError) as exc:
            raise RedisPortError("Redis checkpoint EVAL returned an invalid result") from exc
        if code == -1:
            raise RedisPortError("stale Redis checkpoint version")
        if code == -2:
            raise RedisPortError("Redis checkpoint version conflict")
        if code == -3:
            raise RedisPortError("Redis checkpoint is not a valid versioned snapshot")
        if code not in {0, 1}:
            raise RedisPortError("Redis checkpoint EVAL returned an unknown result")

    @staticmethod
    def _assert_compatible_current(
        current_payload: bytes | str | None,
        state: InvestigationGraphState,
        payload: str,
    ) -> None:
        if current_payload is None:
            return
        try:
            current = InvestigationGraphState.from_canonical_snapshot(_json_value(current_payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPortError("Redis checkpoint is not a valid versioned snapshot") from exc
        if state.checkpoint_version < current.checkpoint_version:
            raise RedisPortError("stale Redis checkpoint version")
        if state.checkpoint_version == current.checkpoint_version and payload != _json_value(current_payload):
            raise RedisPortError("Redis checkpoint version conflict")

    def _save_with_pipeline(self, pipeline_factory: Any, key: str, payload: str, state: InvestigationGraphState) -> bool:
        """Try a redis-py WATCH/MULTI transaction; return False for minimal fake clients."""

        pipeline = pipeline_factory()
        watch = getattr(pipeline, "watch", None)
        multi = getattr(pipeline, "multi", None)
        execute = getattr(pipeline, "execute", None)
        get = getattr(pipeline, "get", None)
        set_value = getattr(pipeline, "set", None)
        if not all(callable(item) for item in (watch, multi, execute, get, set_value)):
            return False
        for _ in range(3):
            try:
                watch(key)
                current_payload = get(key)
                self._assert_compatible_current(current_payload, state, payload)
                multi()
                set_value(key, payload, ex=self._ttl_seconds)
                result = execute()
                # redis-py raises WatchError on a conflict.  Do not treat an arbitrary command
                # failure as a successful compatibility save.
                if result is None:
                    raise RedisPortError("Redis checkpoint transaction returned no result")
                return True
            except Exception as exc:
                if exc.__class__.__name__ not in {"WatchError", "TransactionError"}:
                    raise
        raise RedisPortError("Redis checkpoint transaction conflicted repeatedly")

    def load(self, investigation_id: str) -> InvestigationGraphState | None:
        payload = self._client.get(_key(self._namespace, "checkpoint", investigation_id))
        if payload is None:
            return None
        try:
            return InvestigationGraphState.from_canonical_snapshot(_json_value(payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPortError("Redis checkpoint is not a valid InvestigationGraphState") from exc


class RedisRuntimeEventSink(RuntimeEventSink):
    """Append-only event list scoped to one investigation."""

    def __init__(
        self,
        client: RedisCommandClient,
        *,
        investigation_id: str,
        namespace: str = "insightflow",
    ) -> None:
        self._client = client
        self._key = _key(namespace, "events", investigation_id)

    def emit(self, event: GraphEvent) -> None:
        self._client.rpush(self._key, event.model_dump_json())

    def list_events(self) -> tuple[GraphEvent, ...]:
        events: list[GraphEvent] = []
        for payload in self._client.lrange(self._key, 0, -1):
            try:
                events.append(GraphEvent.model_validate_json(_json_value(payload)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RedisPortError("Redis runtime event is not a valid GraphEvent") from exc
        return tuple(events)


class RedisIdempotencyStore:
    """Atomic request claim store; it intentionally stores only an owner marker, never a result."""

    def __init__(self, client: RedisCommandClient, *, namespace: str = "insightflow") -> None:
        self._client = client
        self._namespace = namespace

    def try_claim(self, key: str, *, owner: str, ttl_seconds: int = 300) -> bool:
        if not owner:
            raise ValueError("idempotency owner cannot be empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        payload = json.dumps({"owner": owner}, ensure_ascii=False, separators=(",", ":"))
        claimed = self._client.set(
            _key(self._namespace, "idempotency", key),
            payload,
            ex=ttl_seconds,
            nx=True,
        )
        return bool(claimed)

    def owner(self, key: str) -> str | None:
        payload = self._client.get(_key(self._namespace, "idempotency", key))
        if payload is None:
            return None
        try:
            value = json.loads(_json_value(payload))
            owner = value.get("owner") if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RedisPortError("Redis idempotency marker is not valid JSON") from exc
        return owner if isinstance(owner, str) else None

    def release(self, key: str, *, owner: str) -> None:
        if self.owner(key) == owner:
            self._client.delete(_key(self._namespace, "idempotency", key))

