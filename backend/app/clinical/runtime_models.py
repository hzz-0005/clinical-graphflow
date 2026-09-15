from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clinical.registry import CLINICAL_TOOL_NAMES


class CallToolAction(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    type:Literal["call_tool"]="call_tool"
    tool:str
    arguments:dict[str,Any]=Field(default_factory=dict)
    rationale:str=Field(min_length=1,max_length=1000)
    hypothesis_id:str|None=None

    @field_validator("tool")
    @classmethod
    def registered_query_tool(cls,value):
        if value not in CLINICAL_TOOL_NAMES or value=="submit_clinical_conclusion": raise ValueError("only registered governed query tools are allowed")
        return value


class FinishAction(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    type:Literal["finish"]="finish"
    conclusion:str=Field(min_length=1,max_length=6000)
    evidence_ids:tuple[str,...]=()
    limitations:tuple[str,...]=()
    inconclusive:bool=False

    @field_validator("evidence_ids")
    @classmethod
    def valid_ids(cls,values):
        import re
        if any(re.fullmatch(r"E\d{2,}",x) is None for x in values): raise ValueError("invalid evidence citation")
        return values


RuntimeAction=Annotated[CallToolAction|FinishAction,Field(discriminator="type")]

class AgentDecision(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    action:RuntimeAction


class InvestigationBrief(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    question:str=Field(min_length=3,max_length=1000)
    intent:Literal["efficacy","safety","exposure","site_quality","data_quality","general","unsupported"]
    trial_id:str|None=None
    metric:str|None=None
    requested_dimensions:tuple[str,...]=()


class RuntimeContext(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    brief:InvestigationBrief
    available_tools:tuple[str,...]
    tool_specs:tuple[dict[str,Any],...]=()
    hypotheses:tuple[str,...]=()
    evidence_summaries:tuple[str,...]=()
    observations:tuple[dict[str,Any],...]=()
    prior_actions:tuple[str,...]=()
    remaining_steps:int=Field(ge=0)
    remaining_queries:int=Field(ge=0)
    data_gaps:tuple[str,...]=()
    # V15 metadata only: actual fields discovered in the selected published
    # version. Payload values never enter the model context.
    data_catalog:dict[str,Any]=Field(default_factory=dict)

    @model_validator(mode="after")
    def tools_are_registered(self):
        if any(x not in CLINICAL_TOOL_NAMES or x=="submit_clinical_conclusion" for x in self.available_tools): raise ValueError("context contains an ungoverned tool")
        return self

