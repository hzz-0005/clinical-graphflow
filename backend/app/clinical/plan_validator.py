from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.clinical.analysis_plan import InvestigationPlan
from app.clinical.registry import ClinicalToolRegistry


# A task dimension normally names an observed data field and therefore must be
# present in the runtime catalog.  Sensitivity analysis is the exception:
# ``analysis_method`` selects a governed calculation (for example complete
# case versus conservative imputation); it is a control parameter, not a
# column that should be discovered in PostgreSQL.
GOVERNED_CONTROL_DIMENSIONS = frozenset({"analysis_method"})
DIMENSION_ALIASES = {
    # Tool argument schemas use compact group names while the semantic catalog
    # exposes the underlying field names.  Accept both at the plan boundary,
    # but bind only to the canonical registry dimension.
    "site": "site_id",
    "arm": "treatment_arm",
    "timepoint": "visit",
}
# Metric discovery searches the semantic registry; dimensions on that task
# describe the user's wording, not a SQL grouping request.  Ignoring them at
# binding time prevents a provider from making a harmless discovery task
# unbindable merely by echoing the requested breakdown.
DIMENSIONLESS_CAPABILITIES = frozenset({"discover_metric"})


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TaskBinding:
    task_id: str
    tool: str


@dataclass(frozen=True)
class ValidatedInvestigationPlan:
    plan: InvestigationPlan
    bindings: tuple[TaskBinding, ...]


class PlanValidator:
    """Resolve abstract analysis capabilities to published, governed tools."""

    def __init__(self, registry: ClinicalToolRegistry) -> None:
        self.registry = registry

    def validate(
        self,
        plan: InvestigationPlan,
        domains: set[str],
        catalog: Mapping[str, Any] | None = None,
    ) -> ValidatedInvestigationPlan:
        catalog_measures = set(catalog.get("measures", ())) if catalog is not None else set()
        catalog_dimensions = set(catalog.get("dimensions", ())) if catalog is not None else set()
        concrete_subgroup_dimensions = {"region", "site_id", "age_band", "sex", "severity_band"}
        bindings: list[TaskBinding] = []
        for task in plan.tasks:
            if catalog is not None:
                if task.measure and catalog_measures and task.measure not in catalog_measures:
                    raise PlanValidationError(
                        f"{task.task_id}: data_catalog_measure_unavailable: {task.measure}"
                    )
                missing_dimensions = {
                    DIMENSION_ALIASES.get(dimension, dimension)
                    for dimension in task.dimensions
                    if catalog_dimensions
                    and DIMENSION_ALIASES.get(dimension, dimension) not in catalog_dimensions
                    and DIMENSION_ALIASES.get(dimension, dimension) not in GOVERNED_CONTROL_DIMENSIONS
                }
                if missing_dimensions:
                    raise PlanValidationError(
                        f"{task.task_id}: data_catalog_dimension_unavailable: "
                        + ",".join(sorted(missing_dimensions))
                    )
            requested_subgroups = set(task.dimensions) & concrete_subgroup_dimensions
            if task.capability == "stratify_measure" and len(requested_subgroups) > 1:
                raise PlanValidationError(
                    f"{task.task_id}: multiple_subgroup_dimensions: "
                    + ",".join(sorted(requested_subgroups))
                )
            requested_dimensions = (
                ()
                if task.capability in DIMENSIONLESS_CAPABILITIES
                else tuple(DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions)
            )
            matches = self.registry.match_capability(
                capability=task.capability,
                operation=task.operation,
                measure=task.measure,
                dimensions=requested_dimensions,
                domains=domains,
            )
            if not matches:
                known_measures = {
                    measure
                    for item in self.registry.capability_specs(domains)
                    if item["capability"] == task.capability
                    for measure in item["measures"]
                }
                reason = "unknown_measure" if task.measure and task.measure not in known_measures else "unsupported_capability"
                raise PlanValidationError(f"{task.task_id}: {reason}: {task.measure or task.capability}")
            bindings.append(TaskBinding(task_id=task.task_id, tool=matches[0].name))
        return ValidatedInvestigationPlan(plan=plan, bindings=tuple(bindings))

