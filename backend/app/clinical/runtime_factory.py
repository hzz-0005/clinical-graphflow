"""Construction and routing of the versioned clinical investigation runtime.

The API owns authentication and published-domain resolution.  This module owns only the next
decision: use the legacy V16 loop or the typed V17 graph.  Keeping that decision in one place
prevents the synchronous endpoint, queued worker and future Temporal activity from drifting apart.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from app.agent.models import InvestigationState
from app.clinical.catalog import CatalogSnapshot
from app.clinical.dynamic_investigator import DynamicClinicalInvestigator
from app.clinical.mcp_gateway import ClinicalMCPGateway
from app.clinical.registry import ClinicalToolRegistry
from app.clinical.runtime_llm import build_runtime_llm
from app.clinical.runtime_port_factory import build_runtime_ports
from app.clinical.telemetry import build_runtime_telemetry
from app.clinical.tools import ClinicalDataScope
from app.clinical.v17_contracts import InvestigationGraphState
from app.clinical.v17_graph import ClinicalInvestigationGraph


def configured_runtime_version(value: str | None = None) -> str:
    """Normalize the deployment switch to ``v16`` or ``v17``.

    ``v17`` is the canonical production runtime.  ``v16`` remains available only as an explicit
    emergency rollback, and numeric aliases are accepted for durable jobs written by the older
    V8 API.  Unknown values fail closed so a typo cannot silently select a different runtime.
    """

    raw = (value if value is not None else os.getenv("INSIGHTFLOW_RUNTIME_VERSION", "v17"))
    normalized = raw.strip().lower()
    if normalized in {"v16", "16", "8", "8.1"}:
        return "v16"
    if normalized in {"v17", "17", "pydantic-ai", "pydantic_ai"}:
        return "v17"
    raise ValueError(
        "unsupported INSIGHTFLOW_RUNTIME_VERSION; expected v16 or v17"
    )


def persisted_runtime_version(version: str | None = None) -> str:
    """Return the compact version marker stored in a durable job request."""

    return "17" if configured_runtime_version(version) == "v17" else "8"


def validate_v16_fallback(*, settings: Any | None = None) -> dict[str, str]:
    """Require an auditable, time-bounded owner for the legacy emergency runtime.

    Runtime selection remains a pure normalizer so old durable job markers can be inspected
    safely.  The actual V16 execution boundary calls this guard; a missing or expired window can
    therefore never turn an ordinary request failure into an implicit legacy fallback.
    """

    def configured(name: str, setting_name: str) -> str:
        value = getattr(settings, setting_name, None) if settings is not None else None
        return str(value if value is not None else os.getenv(name, "")).strip()

    owner = configured("INSIGHTFLOW_V16_FALLBACK_OWNER", "insightflow_v16_fallback_owner")
    reason = configured("INSIGHTFLOW_V16_FALLBACK_REASON", "insightflow_v16_fallback_reason")
    until_raw = configured("INSIGHTFLOW_V16_FALLBACK_UNTIL", "insightflow_v16_fallback_until")
    if not owner or not reason or not until_raw:
        raise ValueError(
            "v16 emergency fallback requires INSIGHTFLOW_V16_FALLBACK_OWNER, "
            "INSIGHTFLOW_V16_FALLBACK_REASON and INSIGHTFLOW_V16_FALLBACK_UNTIL"
        )
    try:
        until = datetime.fromisoformat(until_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("INSIGHTFLOW_V16_FALLBACK_UNTIL must be an ISO-8601 timestamp") from exc
    if until.tzinfo is None:
        raise ValueError("INSIGHTFLOW_V16_FALLBACK_UNTIL must include a timezone")
    until = until.astimezone(timezone.utc)
    if until <= datetime.now(timezone.utc):
        raise ValueError("the configured V16 emergency fallback window has expired")
    return {
        "v16_fallback_owner": owner,
        "v16_fallback_reason": reason,
        "v16_fallback_until": until.isoformat().replace("+00:00", "Z"),
    }


def _scope_catalog(snapshot: CatalogSnapshot, domains: set[str]) -> CatalogSnapshot:
    """Narrow a metadata snapshot to the same domains used by an investigation."""

    datasets = tuple(item for item in snapshot.datasets if item.domain.upper() in domains)
    return snapshot.model_copy(
        update={
            "datasets": datasets,
            "published_domains": tuple(sorted({item.domain for item in datasets})),
            "measures": tuple(sorted({field for item in datasets for field in item.measures})),
            "dimensions": tuple(sorted({field for item in datasets for field in item.dimensions})),
            "data_gaps": tuple(
                item
                for item in snapshot.data_gaps
                if any(dataset.domain in item for dataset in datasets)
            ),
        }
    )


def _catalog_source(runtime: Any, domains: set[str]):
    provider = getattr(runtime, "clinical_data_catalog", None)

    def load(trial_id: str | None, published_batch_id: str | None) -> dict[str, Any]:
        if provider is None:
            return {
                "catalog_version": "V17",
                "trial_id": trial_id,
                "published_batch_id": published_batch_id,
                "published_domains": sorted(domains),
                "datasets": [],
                "measures": [],
                "dimensions": [],
                "data_gaps": ["当前运行时没有可用的数据目录快照"],
            }
        snapshot = provider.snapshot(
            trial_id=trial_id,
            published_batch_id=published_batch_id,
        )
        if isinstance(snapshot, CatalogSnapshot):
            snapshot = _scope_catalog(snapshot, domains)
            return snapshot.model_dump(mode="json")
        # Test doubles and future remote catalogs may already return a JSON-compatible mapping.
        payload = dict(snapshot)
        payload["published_domains"] = sorted(
            set(payload.get("published_domains", ())) & set(domains)
        )
        return payload

    return load


def _matching_checkpoint(
    checkpoint: InvestigationGraphState | None,
    *,
    question: str,
    trial_id: str,
    published_batch_id: str | None,
    enterprise_version: int | None = None,
) -> InvestigationGraphState | None:
    """Accept only a checkpoint that belongs to this exact investigation request.

    Checkpoints contain protected investigation state and are useful for replay, but an
    investigation id must never be enough to let a retried request borrow another question's
    plan or evidence.  The request identity is checked before the graph receives the state;
    a mismatch fails closed instead of silently starting from (or returning) the wrong state.
    """

    if checkpoint is None:
        return None
    if checkpoint.question != question:
        raise ValueError("runtime checkpoint question does not match the request")
    if checkpoint.trial_id != trial_id:
        raise ValueError("runtime checkpoint trial does not match the request")
    if checkpoint.published_batch_id != published_batch_id:
        raise ValueError("runtime checkpoint published batch does not match the request")
    if checkpoint.space != "clinical_trial":
        raise ValueError("runtime checkpoint space does not match the request")
    if enterprise_version is not None:
        # A first run may checkpoint before the repository assigns the initial enterprise CAS
        # version.  Once a canonical row exists, that unbound pre-persist checkpoint is stale and
        # must not override the durable Graph snapshot; a checkpoint bound to a different version
        # remains a hard failure so a stale retry cannot replay over newer clinical state.
        if checkpoint.enterprise_version is None:
            return None
        if checkpoint.enterprise_version != enterprise_version:
            raise ValueError("runtime checkpoint enterprise version does not match the durable graph state")
    return checkpoint


def execute_clinical_investigation(
    *,
    runtime: Any,
    question: str,
    trial_id: str,
    provider: str,
    model: str | None,
    published_batch_id: str | None,
    domains: set[str],
    clinical_scope: ClinicalDataScope,
    runtime_version: str,
    settings: Any,
    investigation_id: str | None = None,
    initial_graph_state: InvestigationGraphState | None = None,
    return_graph_state: bool = False,
):
    """Execute one investigation using the selected versioned runtime."""

    version = configured_runtime_version(runtime_version)
    llm = build_runtime_llm(
        settings,
        provider,
        model,
        runtime_version=version,
    )
    registry = ClinicalToolRegistry(runtime.clinical_tools_factory(clinical_scope))

    if version == "v16":
        fallback = validate_v16_fallback(settings=settings)
        state = DynamicClinicalInvestigator(
            registry,
            llm,
            domains,
            data_catalog=getattr(runtime, "clinical_data_catalog", None),
        ).investigate(question, trial_id, published_batch_id)
        state.audit_metadata.update(fallback)
        return state

    seed_state = InvestigationState(question=question, domain="clinical_trial")
    if investigation_id:
        seed_state.investigation_id = investigation_id
    graph_state = initial_graph_state or InvestigationGraphState.from_legacy_state(
        seed_state,
        trial_id=trial_id,
        published_batch_id=published_batch_id,
        space="clinical_trial",
    )
    _matching_checkpoint(
        graph_state,
        question=question,
        trial_id=trial_id,
        published_batch_id=published_batch_id,
    )
    ports = build_runtime_ports(
        investigation_id=graph_state.investigation_id,
        settings=settings,
    )
    checkpoint = ports.checkpoint_store.load(graph_state.investigation_id)
    resumed = _matching_checkpoint(
        checkpoint,
        question=question,
        trial_id=trial_id,
        published_batch_id=published_batch_id,
        enterprise_version=graph_state.enterprise_version,
    )
    if resumed is not None:
        graph_state = resumed
    telemetry = build_runtime_telemetry(settings=settings)
    graph = ClinicalInvestigationGraph(
        planner=llm,
        gateway=ClinicalMCPGateway(registry, domains),
        catalog=_catalog_source(runtime, domains),
        event_sink=ports.event_sink,
        checkpoint_store=ports.checkpoint_store,
        idempotency_store=ports.idempotency,
        telemetry=telemetry,
    )
    result_graph = graph.run(graph_state)
    result_graph.legacy.audit_metadata.update(
        {
            "runtime_version": "17",
            "catalog_version": "V17",
            "provider": llm.provider,
            "model": llm.model,
            "published_batch_id": published_batch_id,
            "available_domains": sorted(domains),
            "runtime_graph": "typed_clinical_investigation_graph",
            "runtime_ports": ports.mode,
            "telemetry_mode": type(telemetry).__name__,
            "runtime_resume": resumed is not None,
        }
    )
    return result_graph if return_graph_state else result_graph.to_legacy_state()

