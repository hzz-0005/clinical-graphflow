"""Small, deterministic release-boundary scores for the typed V17 runtime."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


class V17CaseScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_correct: bool
    evidence_recall: float = Field(ge=0, le=1)
    route_correct: bool = True
    tool_contract_correct: bool = True
    answer_part_coverage: float = Field(ge=0, le=1)


def score_v17_case(
    *,
    expected_status: str,
    actual_status: str,
    evidence_ids: Sequence[str],
    expected_evidence_ids: Sequence[str] = (),
    expected_route: str | None = None,
    actual_route: str | None = None,
    required_tools: Sequence[str] = (),
    actual_tools: Sequence[str] = (),
    required_answer_parts: Sequence[str] = (),
    covered_answer_parts: Sequence[str] = (),
) -> V17CaseScore:
    """Score facts that can be checked without asking an LLM to judge prose quality.

    A declared ``gap`` is a valid boundary when the case expects a gap and no evidence is
    available.  This keeps "I don't know" distinct from a failed query or an invented answer.
    """

    expected_ids = set(expected_evidence_ids)
    actual_ids = set(evidence_ids)
    if expected_ids:
        evidence_recall = len(expected_ids & actual_ids) / len(expected_ids)
    elif expected_status == "gap" and not actual_ids:
        evidence_recall = 1.0
    else:
        evidence_recall = 1.0 if actual_ids else 0.0

    required = set(required_answer_parts)
    covered = set(covered_answer_parts)
    answer_part_coverage = (
        len(required & covered) / len(required) if required else 1.0
    )
    required_tool_set = set(required_tools)
    actual_tool_set = set(actual_tools)
    route_correct = expected_route is None or expected_route == actual_route
    tool_contract_correct = required_tool_set.issubset(actual_tool_set)
    return V17CaseScore(
        boundary_correct=expected_status == actual_status,
        evidence_recall=evidence_recall,
        route_correct=route_correct,
        tool_contract_correct=tool_contract_correct,
        answer_part_coverage=answer_part_coverage,
    )

