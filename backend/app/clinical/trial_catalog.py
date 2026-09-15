from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict


class ClinicalTrialCatalogItem(BaseModel):
    """A governed, user-facing summary of one analysis-ready clinical trial."""

    model_config = ConfigDict(frozen=True)
    trial_id: str
    title: str
    phase: str | None = None
    status: str | None = None
    primary_endpoint: str | None = None


@dataclass(frozen=True)
class PostgresClinicalTrialCatalog:
    database_url: str

    def list_trials(self) -> list[ClinicalTrialCatalogItem]:
        query = """
            SELECT trial_id,
                   COALESCE(NULLIF(indication, ''), trial_id) AS title,
                   phase,
                   status,
                   primary_endpoint
              FROM analytics_clinical_core.dim_trials
             ORDER BY trial_id
        """
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                return [ClinicalTrialCatalogItem.model_validate(row) for row in cursor.fetchall()]

    def available_domains(self, trial_id: str) -> set[str]:
        """Return only participant domains that actually contain rows for one trial."""

        checks = {
            "DM": "analytics_clinical_core.dim_participants",
            "ADSL": "analytics_clinical_marts.mart_trial_population",
            "ADEFF": "analytics_clinical_marts.mart_week12_efficacy",
            "AE": "analytics_clinical_marts.mart_safety_trend",
            "EX": "analytics_clinical_marts.mart_treatment_exposure",
            # Site-quality is a governed derived mart, not an LB laboratory
            # domain.  Discover it from rows so the planner can expose
            # protocol-deviation and temperature-excursion tools when those
            # signals are actually present.
            "SITE_QUALITY": "analytics_clinical_marts.mart_site_quality",
        }
        domains: set[str] = set()
        with psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                for domain, relation in checks.items():
                    cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {relation} WHERE trial_id = %s)", (trial_id,))
                    if cursor.fetchone()[0]:
                        domains.add(domain)
        return domains

