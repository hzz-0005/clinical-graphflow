from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AnalysisOperation = Literal[
    "discover",
    "describe",
    "compare",
    "rank",
    "trend",
    "stratify",
    "correlate",
    "sensitivity",
    "quality_check",
]


class AnswerRequirement(BaseModel):
    """One independently checkable part of the user's question."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]*$")
    question_part: str = Field(min_length=2, max_length=500)


class AnalysisTask(BaseModel):
    """A governed analytical request; deliberately contains no SQL surface.

    ``hypothesis`` is the provider-authored question that this task is meant to test.
    It is optional for backwards-compatible plans; the runtime supplies an auditable
    fallback statement when an older provider omits it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str = Field(pattern=r"^A[1-9][0-9]*$")
    operation: AnalysisOperation
    measure: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    dimensions: tuple[str, ...] = ()
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    capability: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    hypothesis: str | None = Field(default=None, min_length=4, max_length=500)
    depends_on: tuple[str, ...] = ()
    answers: tuple[str, ...] = Field(min_length=1)


class InvestigationPlan(BaseModel):
    """Validated intermediate representation between natural language and tools."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    question: str = Field(min_length=3, max_length=2000)
    trial_id: str | None = None
    answer_requirements: tuple[AnswerRequirement, ...] = Field(min_length=1, max_length=12)
    tasks: tuple[AnalysisTask, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_graph_and_coverage(self) -> "InvestigationPlan":
        requirement_ids = [item.requirement_id for item in self.answer_requirements]
        task_ids = [item.task_id for item in self.tasks]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("answer requirement ids must be unique")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("analysis task ids must be unique")
        known_requirements = set(requirement_ids)
        known_tasks = set(task_ids)
        for task in self.tasks:
            if not set(task.depends_on).issubset(known_tasks):
                raise ValueError(f"task {task.task_id} depends on an unknown task")
            if task.task_id in task.depends_on:
                raise ValueError(f"task {task.task_id} cannot depend on itself")
            if not set(task.answers).issubset(known_requirements):
                raise ValueError(f"task {task.task_id} references an unknown answer requirement")
        if self.uncovered_requirement_ids:
            raise ValueError("every answer requirement must be covered by at least one analysis task")
        return self

    @property
    def uncovered_requirement_ids(self) -> tuple[str, ...]:
        covered = {requirement for task in self.tasks for requirement in task.answers}
        return tuple(
            item.requirement_id for item in self.answer_requirements if item.requirement_id not in covered
        )


class RequirementAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]*$")
    evidence_ids: tuple[str, ...] = ()
    gap: str | None = Field(default=None, min_length=4, max_length=500)

    @model_validator(mode="after")
    def evidence_or_gap(self) -> "RequirementAnswer":
        if not self.evidence_ids and not self.gap:
            raise ValueError("a requirement answer needs evidence or a specific gap")
        return self


class PlanAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    conclusion: str = Field(min_length=3, max_length=8000)
    coverage: tuple[RequirementAnswer, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    key_findings: tuple[str, ...] = ()
    follow_up: tuple[str, ...] = ()


class PlanRevision(BaseModel):
    """A bounded decision about the not-yet-executed portion of a plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["keep", "replace_remaining", "finish"]
    rationale: str = Field(min_length=2, max_length=1000)
    replacement_tasks: tuple[AnalysisTask, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def replacements_only_when_replacing(self) -> "PlanRevision":
        if self.action != "replace_remaining" and self.replacement_tasks:
            raise ValueError("only replace_remaining may provide replacement tasks")
        return self


def apply_revision(
    plan: InvestigationPlan,
    completed_task_ids: set[str],
    revision: PlanRevision,
) -> InvestigationPlan:
    if revision.action in {"keep", "finish"}:
        return plan
    replacement_ids = {task.task_id for task in revision.replacement_tasks}
    overlap = replacement_ids & completed_task_ids
    if overlap:
        raise ValueError("revision cannot rewrite completed tasks: " + ",".join(sorted(overlap)))
    completed = tuple(task for task in plan.tasks if task.task_id in completed_task_ids)
    return InvestigationPlan(
        question=plan.question,
        trial_id=plan.trial_id,
        answer_requirements=plan.answer_requirements,
        tasks=completed + revision.replacement_tasks,
    )

