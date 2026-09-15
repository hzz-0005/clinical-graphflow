from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.analysis_plan import InvestigationPlan


class GeneralizationCase(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    id:str=Field(pattern=r"^G[0-9]{2}$")
    category:str
    question:str=Field(min_length=5)
    min_answer_parts:int=Field(ge=1)
    expected_capabilities:tuple[str,...]


class GeneralizationScore(BaseModel):
    decomposition_coverage:float=Field(ge=0,le=1)
    capability_recall:float=Field(ge=0,le=1)
    unexpected_capabilities:tuple[str,...]=()


def load_generalization_cases(path:Path)->tuple[GeneralizationCase,...]:
    return tuple(GeneralizationCase.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8")))


def score_plan(case:GeneralizationCase,plan:InvestigationPlan)->GeneralizationScore:
    actual={task.capability for task in plan.tasks}
    expected=set(case.expected_capabilities)
    recall=1.0 if not expected else len(actual&expected)/len(expected)
    return GeneralizationScore(
        decomposition_coverage=min(len(plan.answer_requirements)/case.min_answer_parts,1.0),
        capability_recall=recall,
        unexpected_capabilities=tuple(sorted(actual-expected)),
    )

