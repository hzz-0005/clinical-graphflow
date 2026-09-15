from __future__ import annotations

import json
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

PUBLIC_TOOLS=("search_studies","lookup_drug_label","lookup_faers_signals","summarize_ehr_cohort")


class PublicToolDecision(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    tool: str
    query: str=Field(min_length=1,max_length=200)
    rationale: str=Field(min_length=1,max_length=500)

    @field_validator("tool")
    @classmethod
    def governed_tool(cls,value):
        if value not in PUBLIC_TOOLS: raise ValueError("only governed public clinical tools are allowed")
        return value


class PublicSynthesis(BaseModel):
    model_config=ConfigDict(frozen=True,extra="forbid")
    answer: str=Field(min_length=30,max_length=8000)
    answered_parts: list[str]=Field(min_length=1)
    evidence_ids: list[str]=Field(min_length=1)


class PublicRuntimeLLM(Protocol):
    provider: str
    model: str
    def decide(self, question: str, subject: str, allowed_tools: tuple[str, ...] | None = None) -> PublicToolDecision: ...
    def synthesize(self, *, question: str, evidence: list[dict], required_parts: list[str]) -> PublicSynthesis: ...


Transport=Callable[[str,dict[str,str],dict[str,Any],float],dict[str,Any]]


def _json_object(content: str) -> dict:
    text=(content or "").strip()
    start,end=text.find("{"),text.rfind("}")
    if start<0 or end<start: raise ValueError("model response did not contain a JSON object")
    return json.loads(text[start:end+1])


class OpenAICompatiblePublicRuntimeLLM:
    def __init__(self,provider:str,model:str,api_key:str,base_url:str,timeout:float=60,transport:Transport|None=None):
        if not api_key: raise ValueError(f"{provider} API key is not configured")
        self.provider,self.model=provider,model; self._key=api_key; self._url=base_url.rstrip("/"); self._timeout=timeout; self._transport=transport or self._post

    def decide(self,question:str,subject:str,allowed_tools:tuple[str,...]|None=None)->PublicToolDecision:
        catalog={"search_studies":"研究注册、疾病、干预、阶段、招募和预设终点","lookup_drug_label":"FDA 药品适应证、禁忌、黑框警告和标签不良反应","lookup_faers_signals":"FAERS 药物与不良反应报告共现，不能判断因果","summarize_ehr_cohort":"Synthea 合成患者疾病、用药、就诊和观察队列"}
        if allowed_tools is not None:
            catalog={name:meaning for name,meaning in catalog.items() if name in allowed_tools}
        if not catalog:
            raise ValueError("当前调查空间没有可用的受治理工具")
        prompt={"question":question,"subject":subject,"governed_tools":[{"name":name,"meaning":meaning} for name,meaning in catalog.items()]}
        payload={"model":self.model,"temperature":0,"response_format":{"type":"json_object"},"messages":[{"role":"system","content":"你是临床数据调查路由器。只返回 JSON：{\"tool\":受治理工具名,\"query\":检索词,\"rationale\":中文理由}。rationale 不超过 40 个汉字。不得生成 SQL，不得把注册研究、FDA 标签、FAERS 和合成 EHR 混成同一证据类别。药品安全工具的 query 只能填写药品通用名或商品名，禁止附加不良反应、问题描述或布尔表达式；例如 METFORMIN，不能写 METFORMIN nausea。研究工具可填写疾病或干预主题；EHR 工具可填写队列概念。"},{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}],"max_tokens":1200}
        try:
            data=self._transport(f"{self._url}/chat/completions",{"Authorization":f"Bearer {self._key}","Content-Type":"application/json"},payload,self._timeout)
            return PublicToolDecision.model_validate(_json_object(data["choices"][0]["message"]["content"]))
        except Exception as exc: raise ValueError("public clinical runtime provider returned an invalid decision") from exc

    def synthesize(self, *, question: str, evidence: list[dict], required_parts: list[str]) -> PublicSynthesis:
        prompt={"question":question,"required_parts":required_parts,"evidence":evidence}
        system=("你是临床证据报告撰写器。只返回 JSON：{\"answer\":\"中文结论\",\"answered_parts\":[...],\"evidence_ids\":[...]}. "
                "必须逐项回答 required_parts；把英文监管正文提炼并翻译成普通人能读懂的中文要点，不要大段复制原文。"
                "answer 正文必须原样包含每个已有证据编号，例如 [E01]；evidence_ids 数组不能代替正文引用。"
                "药品标签问题必须实际列出至少三项具体警告、具体禁忌和至少三项具体不良反应，不能只说标签中存在这些栏目。"
                "合成 EHR 问题若证据行含 is_measurement=true，必须列出至少两项观察的名称、患者数、单位和示例值；不要把本次证据包未展示写成数据库中不存在。"
                "每类事实紧邻引用已有 [E##]，不得创造证据编号、数据、发生率或因果关系。"
                "FDA 标签、FAERS 共现和研究注册必须分开说明；FAERS 共现不能写成药物导致反应，注册信息不能写成疗效已证实。"
                "若证据缺少某栏目，明确说明当前数据未提供。")
        payload={"model":self.model,"temperature":0,"response_format":{"type":"json_object"},"messages":[{"role":"system","content":system},{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}],"max_tokens":6000}
        try:
            data=self._transport(f"{self._url}/chat/completions",{"Authorization":f"Bearer {self._key}","Content-Type":"application/json"},payload,self._timeout)
            return PublicSynthesis.model_validate(_json_object(data["choices"][0]["message"]["content"]))
        except Exception as exc: raise ValueError("public clinical runtime provider returned an invalid synthesis") from exc

    @staticmethod
    def _post(url,headers,payload,timeout):
        response=httpx.post(url,headers=headers,json=payload,timeout=timeout);response.raise_for_status();return response.json()


class AnthropicPublicRuntimeLLM(OpenAICompatiblePublicRuntimeLLM):
    def __init__(self,model,api_key,base_url,timeout=60,transport=None): super().__init__("anthropic",model,api_key,base_url,timeout,transport)
    def decide(self,question:str,subject:str,allowed_tools:tuple[str,...]|None=None)->PublicToolDecision:
        catalog=[{"name":"search_studies","meaning":"研究注册"},{"name":"lookup_drug_label","meaning":"FDA 药品标签"},{"name":"lookup_faers_signals","meaning":"FAERS 报告共现"},{"name":"summarize_ehr_cohort","meaning":"Synthea 合成 EHR"}]
        if allowed_tools is not None:
            catalog=[item for item in catalog if item["name"] in allowed_tools]
        if not catalog:
            raise ValueError("当前调查空间没有可用的受治理工具")
        prompt=json.dumps({"question":question,"subject":subject,"governed_tools":catalog},ensure_ascii=False)
        payload={"model":self.model,"system":"只返回 JSON：{\"tool\":工具名,\"query\":检索词,\"rationale\":中文理由}。rationale 不超过 40 个汉字。禁止 SQL，禁止混淆证据类别。药品安全工具的 query 只能填写药品通用名或商品名，不能附加不良反应或问题描述。","messages":[{"role":"user","content":prompt}],"temperature":0,"max_tokens":1200}
        try:
            data=self._transport(f"{self._url}/v1/messages",{"x-api-key":self._key,"anthropic-version":"2023-06-01","Content-Type":"application/json"},payload,self._timeout)
            content="".join(item.get("text","") for item in data.get("content",[]) if item.get("type")=="text")
            return PublicToolDecision.model_validate(_json_object(content))
        except Exception as exc: raise ValueError("anthropic public clinical runtime returned an invalid decision") from exc

    def synthesize(self, *, question: str, evidence: list[dict], required_parts: list[str]) -> PublicSynthesis:
        prompt=json.dumps({"question":question,"required_parts":required_parts,"evidence":evidence},ensure_ascii=False)
        system=("只返回 JSON：{\"answer\":\"中文结论\",\"answered_parts\":[...],\"evidence_ids\":[...]}. "
                "逐项回答 required_parts，将英文证据提炼为中文并引用已有 [E##]。不得创造事实、证据编号、发生率或因果关系。"
                "合成 EHR 证据行若含 is_measurement=true，列出至少两项观察的名称、患者数、单位和示例值；不要把未展示写成不存在。"
                "FDA 标签、FAERS 共现和研究注册必须分开，明确各自限制。")
        payload={"model":self.model,"system":system,"messages":[{"role":"user","content":prompt}],"temperature":0,"max_tokens":2400}
        try:
            data=self._transport(f"{self._url}/v1/messages",{"x-api-key":self._key,"anthropic-version":"2023-06-01","Content-Type":"application/json"},payload,self._timeout)
            content="".join(item.get("text","") for item in data.get("content",[]) if item.get("type")=="text")
            return PublicSynthesis.model_validate(_json_object(content))
        except Exception as exc: raise ValueError("anthropic public clinical runtime returned an invalid synthesis") from exc


def build_public_runtime_llm(settings,provider:str,model:str|None=None)->PublicRuntimeLLM:
    if provider=="anthropic": return AnthropicPublicRuntimeLLM(model or settings.anthropic_model,settings.anthropic_api_key,settings.anthropic_base_url,settings.insightflow_llm_timeout_seconds)
    configs={"openai":(settings.openai_api_key,settings.openai_base_url,model or settings.openai_model),"deepseek":(settings.deepseek_api_key,settings.deepseek_base_url,model or settings.deepseek_model),"glm":(settings.zhipu_api_key,settings.glm_base_url,model or settings.glm_model),"kimi":(settings.moonshot_api_key,settings.kimi_base_url,model or settings.kimi_model),"custom":(settings.custom_llm_api_key,settings.custom_llm_base_url,model or settings.custom_llm_model)}
    if provider not in configs: raise ValueError("provider is not OpenAI-compatible for public runtime")
    key,url,resolved=configs[provider]
    if not resolved: raise ValueError(f"{provider} model is not configured")
    return OpenAICompatiblePublicRuntimeLLM(provider,resolved,key,url,settings.insightflow_llm_timeout_seconds)

