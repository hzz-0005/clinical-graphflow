from __future__ import annotations

from app.clinical.analysis_plan import InvestigationPlan, PlanAnswer


class CoverageError(ValueError):
    pass


class CoverageVerifier:
    """Prevent a fluent conclusion from silently skipping part of the question."""

    def verify(self, plan: InvestigationPlan, answer: PlanAnswer, evidence_ids: set[str]) -> None:
        expected = {item.requirement_id for item in plan.answer_requirements}
        actual = {item.requirement_id for item in answer.coverage}
        missing = expected - actual
        unknown = actual - expected
        if missing:
            raise CoverageError("missing answer requirements: " + ",".join(sorted(missing)))
        if unknown:
            raise CoverageError("unknown answer requirements: " + ",".join(sorted(unknown)))
        cited = {evidence_id for item in answer.coverage for evidence_id in item.evidence_ids}
        invalid = cited - evidence_ids
        if invalid:
            raise CoverageError("unknown evidence ids: " + ",".join(sorted(invalid)))

