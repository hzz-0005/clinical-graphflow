from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException

from app.clinical.adapters.postgres import PostgresClinicalAdapter
from app.clinical.api import create_clinical_router
from app.clinical.data_publication import QuarantinePublicationBridge
from app.clinical.domain_registry import DomainRegistry
from app.clinical.understanding_api import create_understanding_router
from app.clinical.runtime_api import create_dynamic_runtime_router, create_catalog_router
from app.clinical.quarantine import PostgresQuarantineRepository, QuarantineRepository
from app.clinical.import_api import create_clinical_import_router, create_smart_import_router
from app.clinical.import_repository import PostgresCdiscImportRepository
from app.clinical.job_api import create_clinical_job_router
from app.clinical.jobs import ClinicalJobRepository, PostgresClinicalJobRepository
from app.clinical.ingestion import CdiscImportCoordinator, CdiscPublicationService
from app.clinical.investigator import ClinicalInvestigator
from app.clinical.tools import ClinicalDataScope, ClinicalTools
from app.clinical.trial_catalog import PostgresClinicalTrialCatalog
from app.clinical.public_api import create_public_clinical_router
from app.clinical.public_investigation import PostgresPublicClinicalRepository
from app.clinical.catalog import PostgresClinicalDataCatalog
from app.clinical.temporal_api import create_temporal_router
from app.clinical.temporal_outbox import PostgresTemporalSignalOutbox
from app.enterprise.repository import EnterpriseRepository, PostgresEnterpriseRepository
from app.semantic.metrics import ClinicalMetricRepository
from app.settings import get_settings
from app.tools.sql_executor import ReadonlySqlTool
from app.tools.sql_safety import CLINICAL_MART_RELATIONS

DatabaseCheck = Callable[[], None]


@dataclass(frozen=True)
class Runtime:
    clinical_metrics: ClinicalMetricRepository
    clinical_trial_catalog: PostgresClinicalTrialCatalog
    domain_registry: DomainRegistry
    clinical_investigator: ClinicalInvestigator
    clinical_tools_factory: Callable[[ClinicalDataScope], ClinicalTools]
    enterprise_repository: EnterpriseRepository
    cdisc_imports: CdiscImportCoordinator
    cdisc_publications: CdiscPublicationService
    clinical_jobs: ClinicalJobRepository
    quarantine_repository: QuarantineRepository
    ingestion_repository: PostgresCdiscImportRepository
    publication_bridge: QuarantinePublicationBridge
    public_clinical_repository: PostgresPublicClinicalRepository
    clinical_data_catalog: PostgresClinicalDataCatalog
    temporal_signal_outbox: PostgresTemporalSignalOutbox


def check_database() -> None:
    with psycopg.connect(get_settings().backend_database_url, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()


def build_runtime() -> Runtime:
    settings = get_settings()
    metrics = ClinicalMetricRepository.from_yaml(settings.semantic_dir / "clinical_metrics.yml")
    domains = DomainRegistry.from_yaml(settings.semantic_dir / "clinical_domains.yml")

    def tools_factory(scope: ClinicalDataScope) -> ClinicalTools:
        executor = ReadonlySqlTool(
            database_url=settings.backend_database_url,
            allowed_relations=CLINICAL_MART_RELATIONS,
            statement_timeout_ms=settings.sql_statement_timeout_ms,
        )
        return ClinicalTools(metrics, PostgresClinicalAdapter(executor), scope)

    import_repository = PostgresCdiscImportRepository(settings.enterprise_database_url)
    quarantine_repository = PostgresQuarantineRepository(settings.enterprise_database_url)
    return Runtime(
        clinical_metrics=metrics,
        clinical_trial_catalog=PostgresClinicalTrialCatalog(settings.backend_database_url),
        domain_registry=domains,
        clinical_investigator=ClinicalInvestigator(metrics, tools_factory),
        clinical_tools_factory=tools_factory,
        enterprise_repository=PostgresEnterpriseRepository(settings.enterprise_database_url),
        cdisc_imports=CdiscImportCoordinator(settings.cdisc_pseudonym_salt, import_repository),
        cdisc_publications=CdiscPublicationService(import_repository),
        clinical_jobs=PostgresClinicalJobRepository(settings.enterprise_database_url),
        quarantine_repository=quarantine_repository,
        ingestion_repository=import_repository,
        publication_bridge=QuarantinePublicationBridge(quarantine_repository, domains, import_repository, settings.cdisc_pseudonym_salt),
        public_clinical_repository=PostgresPublicClinicalRepository(settings.backend_database_url),
        clinical_data_catalog=PostgresClinicalDataCatalog(settings.backend_database_url, domains),
        temporal_signal_outbox=PostgresTemporalSignalOutbox(settings.enterprise_database_url),
    )


def create_app(
    database_check: DatabaseCheck = check_database,
    runtime_factory: Callable[[], Runtime] = build_runtime,
) -> FastAPI:
    application = FastAPI(title="InsightFlow Clinical API", version="11.0.0")
    runtime: Runtime | None = None

    def get_runtime() -> Runtime:
        nonlocal runtime
        if runtime is None:
            runtime = runtime_factory()
        return runtime

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "project": "insightflow-clinical"}

    @application.get("/ready")
    def ready() -> dict[str, Any]:
        try:
            database_check()
            if not get_runtime().clinical_metrics.names:
                raise RuntimeError("clinical semantic metrics unavailable")
            if not get_runtime().domain_registry.names:
                raise RuntimeError("clinical domain registry unavailable")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="clinical database unavailable") from exc
        return {
            "status": "ready",
            "project": "insightflow-clinical",
            "version": "11.0.0",
            "checks": {
                "database": "ok",
                "semantic_metrics": "ok",
                "domain_registry": "ok",
            },
        }

    application.include_router(create_clinical_router(get_runtime))
    application.include_router(create_clinical_import_router(get_runtime))
    application.include_router(create_smart_import_router(get_runtime))
    application.include_router(create_clinical_job_router(get_runtime))
    application.include_router(create_understanding_router(get_runtime))
    application.include_router(create_dynamic_runtime_router(get_runtime))
    application.include_router(create_catalog_router(get_runtime))
    application.include_router(create_public_clinical_router(get_runtime))
    application.include_router(create_temporal_router(get_runtime))
    return application


app = create_app()

