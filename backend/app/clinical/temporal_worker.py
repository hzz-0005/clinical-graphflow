"""Opt-in Temporal Worker entry point for InsightFlow Clinical V21."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.clinical.temporal_activity import build_temporal_activities
from app.clinical.temporal_workflow import build_temporal_worker
from app.settings import get_settings


async def run_temporal_worker(
    *,
    settings: Any | None = None,
    client: Any | None = None,
    runtime_factory: Callable[[], Any] | None = None,
) -> None:
    """Connect to the configured Temporal service and block on the V21 worker.

    The function deliberately refuses to masquerade as a local worker when the optional SDK is
    absent.  Tests can inject a client/runtime factory; production uses the official client and
    the same runtime factory as HTTP/lease workers.
    """

    settings = settings or get_settings()
    if not bool(getattr(settings, "insightflow_temporal_enabled", False)):
        raise RuntimeError(
            "Temporal worker is disabled; set INSIGHTFLOW_TEMPORAL_ENABLED=true to start it"
        )
    if client is None:
        try:
            from temporalio.client import Client
        except ImportError as exc:  # pragma: no cover - deployment-only path
            raise RuntimeError(
                "Temporal worker requires temporalio; install insightflow-backend[temporal]"
            ) from exc
        client = await Client.connect(
            settings.insightflow_temporal_target,
            namespace=settings.insightflow_temporal_namespace,
        )

    worker = build_temporal_worker(
        client,
        task_queue=settings.insightflow_temporal_task_queue,
        activities=build_temporal_activities(runtime_factory=runtime_factory),
    )
    await worker.run()


def main() -> None:
    import asyncio

    asyncio.run(run_temporal_worker())


if __name__ == "__main__":
    main()

