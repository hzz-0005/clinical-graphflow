from __future__ import annotations

import os
import time
from threading import Event, Thread
from datetime import datetime, timezone
from typing import Any, Callable

from app.clinical.api import ClinicalInvestigationRequest, execute_clinical_investigation
from app.clinical.jobs import ClinicalJobRepository
from app.clinical.operations import OPERATIONS
from app.enterprise.api import DEV_USERS


def dynamic_job_runtime_version(request: dict[str, Any]) -> str | None:
    """Resolve the runtime for a dynamic job without making V16 implicit.

    Dynamic job rows identify themselves with the V8 provider/domain fields.  A missing marker
    therefore means "use the configured canonical runtime" (V17 by default), while a persisted
    ``runtime_version=8`` remains an explicit V16 rollback.  The older V5 job shape returns
    ``None`` and is handled by its existing compatibility executor.
    """

    is_dynamic_request = any(
        key in request for key in {"provider", "available_domains", "published_batch_id"}
    )
    if not is_dynamic_request and "runtime_version" not in request:
        return None
    from app.clinical.runtime_factory import configured_runtime_version

    return configured_runtime_version(request.get("runtime_version"))


class ClinicalJobWorker:
    def __init__(
        self,
        repository: ClinicalJobRepository,
        execute: Callable,
        worker_id: str,
        lease_seconds: float = 60,
        max_attempts: int = 3,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._repository = repository
        self._execute = execute
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    @staticmethod
    def _retryable_graph_job(job: Any, exc: Exception) -> bool:
        """Retry only infrastructure/claim failures from the canonical V17 Graph path.

        V16 and the older V5 job shape retain their existing terminal-failure behavior.  V17
        Graph failures are deliberately split: contract/data errors (``ValueError`` and
        ``TypeError``) fail closed, while a lost provider/database connection or a competing
        Graph claim is requeued so the lease/reclaim mechanism can make progress.
        """

        try:
            runtime_version = dynamic_job_runtime_version(job.request)
        except (KeyError, TypeError, ValueError):
            # A malformed/unknown marker is a deterministic job contract failure.
            return False
        if runtime_version != "v17":
            return False
        return isinstance(exc, (ConnectionError, TimeoutError, OSError, RuntimeError)) or exc.__class__.__module__.startswith("psycopg")

    def run_once(self, now: datetime | None = None) -> bool:
        started = time.perf_counter()
        job = self._repository.claim_next(self._worker_id, now or datetime.now(timezone.utc), self._lease_seconds)
        if job is None:
            return False
        stop_heartbeat = Event()

        def renew_lease() -> None:
            interval = max(0.01, self._lease_seconds / 3)
            while not stop_heartbeat.wait(interval):
                try:
                    current = self._repository.get(job.job_id)
                    self._repository.heartbeat(job.job_id, self._worker_id, current.version, datetime.now(timezone.utc), self._lease_seconds)
                except (KeyError, ValueError):
                    return

        heartbeat = Thread(target=renew_lease, name=f"lease-{job.job_id}", daemon=True)
        heartbeat.start()
        try:
            investigation_id = self._execute(job)
            stop_heartbeat.set(); heartbeat.join()
            current = self._repository.get(job.job_id)
            target = "cancelled" if current.cancel_requested else "succeeded"
            self._repository.transition(job.job_id, current.version, target, investigation_id=investigation_id if target == "succeeded" else None)
            OPERATIONS.record_event("durable_worker", target, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            stop_heartbeat.set(); heartbeat.join()
            current = self._repository.get(job.job_id)
            if current.status == "running":
                retryable = self._retryable_graph_job(job, exc)
                if retryable and job.attempt < self._max_attempts:
                    requeue = getattr(self._repository, "requeue", None)
                    if callable(requeue):
                        requeue(job.job_id, current.version, error_code=type(exc).__name__)
                        OPERATIONS.record_event("durable_worker", "retrying", (time.perf_counter() - started) * 1000, retry=True)
                        return True
                self._repository.transition(job.job_id, current.version, "failed", error_code=type(exc).__name__)
            OPERATIONS.record_event("durable_worker", "failed", (time.perf_counter() - started) * 1000, retry=False)
        return True


def main() -> None:
    from app.main import build_runtime

    runtime = build_runtime()
    worker_id = os.getenv("HOSTNAME", "clinical-worker")

    def execute(job) -> str:
        actor = DEV_USERS[job.owner_user_id]
        # Dynamic V8 jobs carry the provider/domain fields.  Their marker is persisted at
        # creation time, but unmarked dynamic requests resolve to the V17 canonical runtime.
        requested_version = dynamic_job_runtime_version(job.request)
        if requested_version is not None:
            from app.clinical.runtime_api import DynamicInvestigationBody, execute_dynamic, persist_dynamic

            payload = dict(job.request)
            payload.pop("runtime_version", None)
            state = execute_dynamic(
                runtime,
                actor,
                DynamicInvestigationBody.model_validate(payload),
                requested_version,
                investigation_id=job.job_id if requested_version == "v17" else None,
                return_graph_state=requested_version == "v17",
            )
            return persist_dynamic(runtime, actor, state, job.job_id)
        body = ClinicalInvestigationRequest.model_validate(job.request)
        item = execute_clinical_investigation(runtime, actor, body, job.job_id)
        return item.investigation_id

    worker = ClinicalJobWorker(runtime.clinical_jobs, execute, worker_id)
    while True:
        if not worker.run_once():
            time.sleep(1)


if __name__ == "__main__":
    main()

