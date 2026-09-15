from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.semantic.search import relevance


class MetricDefinition(BaseModel):
    name: str
    label: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    owner: str
    entity: str
    model: str
    time_dimension: str
    time_grain: str
    dimensions: list[str] = Field(default_factory=list)
    calculation: dict
    rules: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    window: str | None = None

    def allows_dimension(self, dimension: str) -> bool:
        return dimension in self.dimensions


class MetricMatch(BaseModel):
    metric: MetricDefinition
    score: float


class MetricRepository:
    def __init__(self, metrics: list[MetricDefinition]) -> None:
        self._metrics = {metric.name: metric for metric in metrics}

    @classmethod
    def from_yaml(cls, path: Path) -> MetricRepository:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls([MetricDefinition.model_validate(item) for item in payload["metrics"]])

    def get(self, name: str) -> MetricDefinition:
        try:
            return self._metrics[name]
        except KeyError as exc:
            raise KeyError(f"Unknown governed metric: {name}") from exc

    def search(self, query: str, limit: int = 5) -> list[MetricMatch]:
        matches = []
        for metric in self._metrics.values():
            score = relevance(
                query,
                [
                    (metric.name.replace("_", " "), 5),
                    (metric.label, 4),
                    *((alias, 5) for alias in metric.aliases),
                    (metric.description, 1),
                    *((dimension, 0.25) for dimension in metric.dimensions),
                ],
            )
            if score > 0:
                matches.append(MetricMatch(metric=metric, score=score))
        return sorted(matches, key=lambda match: (-match.score, match.metric.name))[:limit]


class ClinicalMetricDefinition(MetricDefinition):
    domain: str = "clinical_trial"
    version: str
    population: str
    timepoint: str | None = None
    estimand: str
    forbidden_interpretations: list[str] = Field(default_factory=list)


class ClinicalMetricRepository(MetricRepository):
    def __init__(self, metrics: list[ClinicalMetricDefinition]) -> None:
        names = [metric.name for metric in metrics]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate governed metric name in clinical domain")
        super().__init__(metrics)

    @classmethod
    def from_yaml(cls, path: Path) -> ClinicalMetricRepository:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            [ClinicalMetricDefinition.model_validate(item) for item in payload["metrics"]]
        )

    @property
    def names(self) -> set[str]:
        return set(self._metrics)

    def get(self, name: str) -> ClinicalMetricDefinition:
        return super().get(name)  # type: ignore[return-value]

