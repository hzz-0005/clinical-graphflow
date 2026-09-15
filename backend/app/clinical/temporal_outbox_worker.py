"""Scheduled Temporal approval-signal delivery for InsightFlow Clinical V22.

V21 exposed a protected HTTP endpoint so an operator could replay the outbox manually.  V22
keeps that endpoint for emergency intervention and adds a small, single-purpose process that
polls PostgreSQL, sends metadata-only signals through the Temporal boundary, and exits cleanly
on shutdown.  It never reads or writes clinical evidence; the existing dispatcher owns all
approval/version checks and retry/dead-letter semantics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.temporal_boundary import TemporalClinicalWorkflowBoundary
from app.clinical.temporal_outbox import TemporalOutboxDispatcher
from app.settings import get_settings

LOGGER = logging.getLogger(__name__)


class TemporalOutboxWorkerResult(BaseModel):
    """Small operational summary; no question, SQL, evidence, or patient data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cycles: int = Field(ge=0)
    dispatched: int = Field(ge=0)
    failed: int = Field(ge=0)
    dispatcher_errors: int = Field(ge=0)


def _setting(settings: Any, name: str, default: Any) -> Any:
    return getattr(settings, name, default)


async def _wait_between_cycles(
    *,
    stop_event: asyncio.Event | None,
    seconds: float,
    sleep: Callable[[float], Awaitable[None]] | None,
) -> bool:
    """Wait for the next poll and return whether shutdown was requested."""

    if sleep is not None:
        # Tests and embedding applications can inject a deterministic clock.  They can set the
        # event from the injected sleep callback without a real-time delay.
        await sleep(seconds)
        return bool(stop_event and stop_event.is_set())
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def run_temporal_outbox_worker(
    *,
    settings: Any | None = None,
    runtime: Any | None = None,
    boundary: Any | None = None,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_cycles: int | None = None,
) -> TemporalOutboxWorkerResult:
    """Poll and dispatch the approval outbox until stopped.

    ``max_cycles`` is intentionally available for smoke tests and one-shot Kubernetes jobs.  The
    normal CLI leaves it unset and runs until SIGTERM cancels the asyncio task.  A real Temporal
    client is connected only when no boundary is injected, so unit tests do not need a cluster.
    """

    settings = settings or get_settings()
    if not bool(_setting(settings, "insightflow_temporal_enabled", False)):
        raise RuntimeError(
            "Temporal outbox worker is disabled; set INSIGHTFLOW_TEMPORAL_ENABLED=true to start it"
        )
    if max_cycles is not None and max_cycles < 1:
        raise ValueError("max_cycles must be positive when provided")

    if runtime is None:
        from app.main import build_runtime

        runtime = build_runtime()
    outbox = getattr(runtime, "temporal_signal_outbox", None)
    repository = getattr(runtime, "enterprise_repository", None)
    if outbox is None or not hasattr(outbox, "pending"):
        raise RuntimeError("Temporal outbox worker requires a temporal_signal_outbox")
    if repository is None or not hasattr(repository, "get_approval"):
        raise RuntimeError("Temporal outbox worker requires an enterprise approval repository")
    # ``Protocol`` checks are not runtime-safe unless decorated with @runtime_checkable; explicit
    # method checks give a useful error for malformed integrations while accepting test doubles.
    missing = [name for name in ("enqueue", "mark_sent", "mark_failed") if not hasattr(outbox, name)]
    if missing:
        raise RuntimeError(f"Temporal outbox worker outbox is missing methods: {', '.join(missing)}")

    if boundary is None:
        boundary = await TemporalClinicalWorkflowBoundary.connect(
            str(_setting(settings, "insightflow_temporal_target", "localhost:7233")),
            namespace=str(_setting(settings, "insightflow_temporal_namespace", "default")),
            task_queue=str(_setting(settings, "insightflow_temporal_task_queue", "clinical-v20")),
        )

    dispatcher = TemporalOutboxDispatcher(
        outbox,
        boundary_factory=lambda _: boundary,
        approval_repository=repository,
        settings=settings,
        max_attempts=int(_setting(settings, "insightflow_temporal_outbox_max_attempts", 5)),
    )
    poll_seconds = float(_setting(settings, "insightflow_temporal_outbox_poll_seconds", 5.0))
    if poll_seconds <= 0:
        raise ValueError("insightflow_temporal_outbox_poll_seconds must be positive")
    batch_size = int(_setting(settings, "insightflow_temporal_outbox_batch_size", 20))
    if batch_size < 1:
        raise ValueError("insightflow_temporal_outbox_batch_size must be positive")

    cycles = 0
    dispatched = 0
    failed = 0
    dispatcher_errors = 0
    while not stop_event or not stop_event.is_set():
        if max_cycles is not None and cycles >= max_cycles:
            break
        cycles += 1
        try:
            events = await dispatcher.dispatch_pending(limit=batch_size)
        except Exception:  # pragma: no cover - defensive boundary for a broken database adapter
            dispatcher_errors += 1
            LOGGER.exception("Temporal outbox dispatch cycle failed")
        else:
            dispatched += sum(event.status == "sent" for event in events)
            failed += sum(event.status in {"pending", "dead"} for event in events)
        if max_cycles is not None and cycles >= max_cycles:
            break
        if await _wait_between_cycles(stop_event=stop_event, seconds=poll_seconds, sleep=sleep):
            break

    return TemporalOutboxWorkerResult(
        cycles=cycles,
        dispatched=dispatched,
        failed=failed,
        dispatcher_errors=dispatcher_errors,
    )


async def _run_forever() -> None:
    await run_temporal_outbox_worker()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run_forever())
    except KeyboardInterrupt:  # pragma: no cover - command-line shutdown
        LOGGER.info("Temporal outbox worker stopped")


if __name__ == "__main__":
    main()

