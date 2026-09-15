from __future__ import annotations

from typing import Callable, Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.clinical.public_investigation import PublicClinicalInvestigator, PublicSpace
from app.enterprise.api import principal_from_header
from app.enterprise.models import Role
from app.clinical.public_runtime_llm import build_public_runtime_llm
from app.settings import get_settings


DATA_SPACES = (
    {"space":"clinical_trial","title":"受试者级临床试验分析","source":"已发布 CDISC/ADaM 数据","data_reality":"由数据发布方决定；演示库为合成数据","what_it_is":"随机分组、疗效、安全性、暴露和研究中心质量数据","examples":["Week-12 治疗组与对照组差异","哪个中心的数据质量负担最高"],"can_answer":"试验内部的描述性疗效、安全性、缺失和中心差异","cannot_prove":"未经统计方案和人工医学审核的因果结论或个体诊疗建议"},
    {"space":"study_registry","title":"临床研究注册库（全球 + 中国地点）","source":"ClinicalTrials.gov（全球公开样本 + 中国地点筛选样本；研究编号已去重）","data_reality":"真实公开研究注册元数据","what_it_is":"研究疾病、干预措施、阶段、招募状态、样本规模、申办方和预设终点","examples":["有哪些二甲双胍糖尿病研究","乳腺癌三期研究采用什么干预"],"can_answer":"哪些研究存在、研究设计和预设研究内容","cannot_prove":"药物有效、最终结果显著或患者个体获益"},
    {"space":"drug_label","title":"FDA 药品标签","source":"openFDA Drug Label","data_reality":"真实公开监管标签文本","what_it_is":"具体药品的适应证、给药途径、禁忌、黑框警告和不良反应说明","examples":["某药有哪些黑框警告","某药标签适应证是什么"],"can_answer":"FDA 标签如何描述某种药物","cannot_prove":"真实世界发生率、比较疗效或个体用药建议"},
    {"space":"safety_signal","title":"药品安全自发报告信号","source":"openFDA FAERS","data_reality":"真实去标识化自发报告聚合","what_it_is":"具体药品名称与报告中不良反应术语的共同出现次数","examples":["某药最常与哪些反应共同报告","严重报告共现有多少"],"can_answer":"当前下载样本内有哪些报告共现信号","cannot_prove":"药物导致该反应、风险发生率或药物间风险高低"},
    {"space":"synthetic_ehr","title":"合成电子健康记录队列","source":"Synthea","data_reality":"合成虚拟患者，不是真实患者","what_it_is":"虚拟患者的疾病、用药、就诊、操作、检验观察和设备记录","examples":["糖尿病虚拟患者有多少","相关队列有多少就诊和用药记录"],"can_answer":"验证跨域患者队列查询、时间线和工具编排","cannot_prove":"真实人群患病率、治疗效果或医疗结论"},
)


class PublicInvestigationBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    question: str=Field(min_length=5,max_length=1000)
    subject: str=Field(min_length=1,max_length=200)
    space: PublicSpace | None=None
    provider: Literal["fake","openai","anthropic","deepseek","glm","kimi","custom"]="fake"
    model: str | None=Field(default=None,max_length=200)
    reference_catalog_version: str | None=Field(default=None,max_length=40)


def create_public_clinical_router(get_runtime: Callable) -> APIRouter:
    router=APIRouter(prefix="/api/v10/clinical")

    @router.get("/data-spaces")
    def data_spaces(x_insightflow_user: str | None=Header(default=None)):
        principal_from_header(x_insightflow_user)
        inventory=get_runtime().public_clinical_repository.inventory()
        return [{**item,"inventory":inventory.get(item["space"],{})} for item in DATA_SPACES]

    @router.post("/public-investigations")
    def investigate(body: PublicInvestigationBody, x_insightflow_user: str | None=Header(default=None)):
        principal=principal_from_header(x_insightflow_user)
        if principal.role is Role.VIEWER:
            raise HTTPException(403,detail={"code":"permission_denied"})
        try:
            planner=None if body.provider=="fake" else build_public_runtime_llm(get_settings(),body.provider,body.model)
            state=PublicClinicalInvestigator(get_runtime().public_clinical_repository,planner).investigate(
                body.question,
                body.subject,
                body.space,
                reference_catalog_version=body.reference_catalog_version,
            )
            return state.model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(422,detail={"code":"public_investigation_invalid","message":str(exc)}) from exc

    return router

