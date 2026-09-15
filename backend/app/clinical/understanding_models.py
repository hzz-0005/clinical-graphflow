from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FieldMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    target: str
    transform: Literal["identity", "trim", "uppercase", "lowercase", "integer", "decimal", "date", "datetime", "boolean"] = "identity"


class FileMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    file_id: str
    source_filename: str
    target_domain: str
    target_version: str | None = None
    status: Literal["standard_candidate", "custom_candidate"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=1000)
    fields: tuple[FieldMapping, ...] = ()

    @model_validator(mode="after")
    def no_duplicate_fields(self):
        if len({x.source for x in self.fields}) != len(self.fields) or len({x.target for x in self.fields}) != len(self.fields):
            raise ValueError("mapping sources and targets must be unique")
        return self


class MappingContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    batch_id: str
    files: tuple[FileMapping, ...] = Field(min_length=1)
    provider: str
    model: str
    visibility_level: str
    registry_version: str = "clinical-domains-v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UnderstandingAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tool: str
    arguments: dict = Field(default_factory=dict)

