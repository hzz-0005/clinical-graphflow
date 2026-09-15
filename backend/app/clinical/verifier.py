from __future__ import annotations

import re

from app.agent.models import EvidenceType, InvestigationState
from app.clinical.models import ClinicalAnalysisSummary, ClinicalVerificationReport
from app.clinical.statistics import evaluate_guardrails


class ClinicalConclusionVerifier:
    """Combine statistical checks with evidence and language governance."""

    REQUIRED_EVIDENCE = {
        EvidenceType.CLINICAL_OUTCOME,
        EvidenceType.CLINICAL_BALANCE,
        EvidenceType.CLINICAL_MISSINGNESS,
        EvidenceType.CLINICAL_SITE,
        EvidenceType.CLINICAL_EXPOSURE,
        EvidenceType.CLINICAL_PROTOCOL_QUALITY,
    }

    def verify(self, state: InvestigationState) -> ClinicalVerificationReport:
        payload = state.baseline.get("clinical_analysis")
        if payload is None:
            raise ValueError("Clinical analysis summary is required for verification")
        report = evaluate_guardrails(ClinicalAnalysisSummary.model_validate(payload))
        flags = list(report.flags)
        answer = state.answer or ""

        evidence_types = {item.evidence_type for item in state.evidence}
        if not self.REQUIRED_EVIDENCE <= evidence_types:
            flags.append("incomplete_evidence_chain")

        cited = set(re.findall(r"\[(E\d{2,})\]", answer))
        existing = {item.evidence_id for item in state.evidence}
        required_ids = {
            item.evidence_id
            for item in state.evidence
            if item.evidence_type in self.REQUIRED_EVIDENCE
        }
        if not cited or not cited <= existing or not required_ids <= cited:
            flags.append("invalid_citations")

        lowered = answer.lower()
        if re.search(r"\basia\s+(?:caused|causes|is the cause)|亚洲(?:地区|人群)?(?:导致|造成)", lowered):
            flags.append("region_as_cause")
        if re.search(r"should (?:take|increase|decrease)|建议用药|调整剂量|增加剂量|减少剂量", lowered):
            flags.append("treatment_advice")
        if state.cost.queries > state.budget.max_queries:
            flags.append("query_budget_exhausted")
        if state.cost.steps > state.budget.max_steps:
            flags.append("step_budget_exhausted")

        unique_flags = list(dict.fromkeys(flags))
        return report.model_copy(
            update={"passed": report.passed and not unique_flags, "flags": unique_flags}
        )

