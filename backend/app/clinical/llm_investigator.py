from __future__ import annotations

from app.agent.models import InvestigationState, InvestigationStatus
from app.clinical.llm import ClinicalLLMClient
from app.clinical.tools import ClinicalDataScope

REQUIRED_INVESTIGATION_TOOLS = {
    "inspect_trial",
    "compare_treatment_effect",
    "check_randomization_balance",
    "analyze_missingness",
    # Site drill-down *capability*, not a named site: a pre-execution plan cannot know which center
    # is abnormal. The V8 dynamic runtime selects site-level execution checks
    # (inspect_treatment_exposure / inspect_protocol_quality) from observed results.
    "profile_sites",
}


class LLMClinicalInvestigator:
    """LLM plans the bounded investigation; deterministic code executes and verifies it."""

    def __init__(self, deterministic_investigator, client: ClinicalLLMClient) -> None:
        self._investigator = deterministic_investigator
        self._client = client

    def investigate(
        self, question: str, scope: ClinicalDataScope, subgroup: str | None = None
    ) -> InvestigationState:
        trial_id = sorted(scope.trial_ids)[0]
        requested = (subgroup or "").strip()
        region = requested or (sorted(scope.regions)[0] if scope.regions else None)
        try:
            result = self._client.plan(question, trial_id, region)
            selected = [action.tool for action in result.plan.actions]
            missing = REQUIRED_INVESTIGATION_TOOLS - set(selected)
            if missing:
                state = InvestigationState(question=question, domain="clinical_trial", status=InvestigationStatus.RUNNING)
                state.llm_usage = result.usage
                state.audit_metadata = {"llm_provider": result.provider, "llm_model": result.model, "llm_plan_tools": selected}
                state.answer = "LLM plan is missing required governed checks: " + ", ".join(sorted(missing))
                state.status = InvestigationStatus.INCONCLUSIVE
                return state
            # The caller's subgroup must reach the executor: it already drove the authorization
            # check, so ignoring it here would analyse a different population than the one that
            # was approved.
            state = self._investigator.investigate(question, scope, subgroup)
            state.llm_usage = result.usage
            state.audit_metadata.update({"llm_provider": result.provider, "llm_model": result.model, "llm_request_id": result.request_id, "llm_plan_tools": selected})
            return state
        except Exception as exc:
            state = InvestigationState(question=question, domain="clinical_trial", status=InvestigationStatus.RUNNING)
            state.fail(f"Clinical LLM planning failed safely: {type(exc).__name__}")
            return state

