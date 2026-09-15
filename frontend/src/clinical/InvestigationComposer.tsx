import { useEffect, useState } from "react";
import { Button, Tag } from "@carbon/react";
import type { CdiscPublishedBatch, ClinicalDataSpace, ClinicalTrialCatalogItem, DynamicState, InvestigationSpace, Role } from "../types";
import { listClinicalDataSpaces, listClinicalTrials, listPublishedCdiscBatches, runDynamicInvestigation, runPublicClinicalInvestigation } from "../api";
import { EvidenceChain } from "./EvidenceChain";
import "./clinical.css";

const domains = (batch?: CdiscPublishedBatch) =>
  batch
    ? Object.entries(batch.domain_coverage ?? {})
        .filter(([, ok]) => ok)
        .map(([domain]) => domain.toUpperCase())
    : [];

const toolLabels:Record<string,string>={
  lookup_faers_signals:'查询不良事件报告',
  lookup_drug_label:'核对 FDA 药品标签',
  search_studies:'查找相关注册研究',
  compare_study_designs:'比较研究阶段和设计',
  summarize_ehr_cohort:'统计虚拟患者队列',
  profile_ehr_concepts:'汇总队列疾病、用药与操作',
  inspect_treatment_exposure:'比较实际剂量与计划剂量',
  inspect_safety_summary:'比较两组安全性事件比例',
  inspect_protocol_quality:'核查方案执行质量',
  analyze_missingness:'检查结局缺失',
  analyze_visit_windows:'检查访视窗口',
  compare_treatment_effect:'比较治疗组与对照组',
  check_randomization_balance:'检查治疗前基线平衡',
};

const hypothesisStatusLabels:Record<string,string>={
  proposed:'已提出（尚未查询）',
  testing:'查询验证中',
  supported:'证据支持',
  rejected:'证据反驳',
  inconclusive:'证据不足',
};

const hypothesisTraceLabels:Record<string,string>={
  hypothesis_proposed:'提出待验证假设',
  hypothesis_proposed_fallback:'补建待验证假设',
  query_executed:'调用受治理工具查询',
  evidence_recorded:'记录查询证据并更新状态',
};

/**
 * 将模型返回的直接回答整理成可读段落。
 *
 * 模型有时会把多个结论、证据引用和局限压在同一行。这里仅按自然语言边界换段，
 * 不修改原文、不拆开紧跟在句号后的证据编号（例如“结论。[E01]”）。这样报告仍可
 * 逐字追溯到模型回答，同时避免 UI 出现一整屏难以阅读的长句。
 */
function answerParagraphs(value: string): string[] {
  const normalized = value.replace(/\r\n?/g, "\n").trim();
  if (!normalized) return [];
  return normalized
    .split(/\n{2,}/u)
    .flatMap((block) => {
      const compact = block.trim();
      if (!compact) return [];
      return compact.split(/(?<=[。！？])(?=\s*(?:[\u4e00-\u9fffA-Za-z]))|(?<=\])\s+(?=[\u4e00-\u9fff])/u);
    })
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
}

function StructuredReport({report}: {report: NonNullable<DynamicState['report']>}) {
  const mode = report.synthesis_mode === 'external_verified' ? '模型回答已通过证据覆盖校验' : report.synthesis_mode === 'inconclusive' ? '证据不足，未强行下结论' : '受治理规则整理';
  const section = (title: string, items: string[]) => items.length > 0 ? <article><h3>{title}</h3><ul>{items.map((item,index)=><li key={`${title}-${index}`}>{item}</li>)}</ul></article> : null;
  const directAnswer = answerParagraphs(report.direct_answer);
  return <section className="structured-report" aria-label="结构化调查报告">
    <div className="structured-report-head"><span>结构化报告（Structured report）</span><small>{mode}</small></div>
    <article className="report-direct"><h3>直接回答</h3>{directAnswer.map((paragraph,index)=><p key={`direct-answer-${index}`}>{paragraph}</p>)}</article>
    <div className="report-sections">
      {section('关键发现', report.key_findings)}
      {section('证据摘要', report.evidence_summary)}
      {section('局限', report.limitations)}
      {section('后续核查', report.follow_up)}
    </div>
  </section>;
}

function HypothesisReview({result}: {result: DynamicState}) {
  const trace = Array.isArray(result.audit_metadata?.hypothesis_trace)
    ? (result.audit_metadata.hypothesis_trace as Array<Record<string, unknown>>)
    : [];
  return <section className="hypothesis-review" aria-label="假设与验证">
    <header>
      <div>
        <h2>假设与验证</h2>
        <p>这里展示查询前提出的命题，以及查询结果如何支持、反驳或暂时无法判断它。</p>
      </div>
      <span>{result.hypotheses.length} 个假设</span>
    </header>
    <p className="hypothesis-flow" aria-label="假设验证流程">
      <strong>提出假设</strong><span>→</span><strong>调用受治理工具</strong><span>→</span><strong>记录证据</strong><span>→</span><strong>支持、反驳或证据不足</strong>
    </p>
    {trace.length > 0 && <div className="hypothesis-trace" aria-label="假设审计轨迹">
      <h3>实际执行顺序（审计轨迹）</h3>
      <ol>{trace.map((event, index) => <li key={`${String(event.stage)}-${String(event.task_id ?? '')}-${index}`}>
        <span>{index + 1}</span>
        <div>
          <strong>{hypothesisTraceLabels[String(event.stage)] ?? String(event.stage)}</strong>
          <small>{event.hypothesis_id ? `${String(event.hypothesis_id)}${event.tool ? ` · ${toolLabels[String(event.tool)] ?? String(event.tool)}` : ''}` : ''}</small>
        </div>
      </li>)}</ol>
    </div>}
    <div className="hypothesis-list">
      {result.hypotheses.map((hypothesis) => <article className={`hypothesis-card status-${hypothesis.status}`} key={hypothesis.hypothesis_id}>
        <div className="hypothesis-card-head">
          <strong>{hypothesis.hypothesis_id}</strong>
          <Tag>{hypothesisStatusLabels[hypothesis.status] ?? hypothesis.status}</Tag>
        </div>
        <p className="hypothesis-statement"><b>提出假设：</b>{hypothesis.statement}</p>
        <div className="hypothesis-facts">
          <p><b>支持证据：</b>{hypothesis.evidence_ids.length ? hypothesis.evidence_ids.join('、') : '无'}</p>
          <p><b>反驳证据：</b>{hypothesis.counter_evidence_ids.length ? hypothesis.counter_evidence_ids.join('、') : '无'}</p>
          {hypothesis.rationale && <p><b>验证理由：</b>{hypothesis.rationale}</p>}
        </div>
      </article>)}
    </div>
  </section>;
}

export function InvestigationComposer({
  role,
  onCompleted,
}: {
  role: Role;
  onCompleted: (state: DynamicState) => void;
}) {
  const [question, setQuestion] = useState("");
  const [space,setSpace]=useState<InvestigationSpace>('clinical_trial');
  const [dataSpaces,setDataSpaces]=useState<ClinicalDataSpace[]>([]);
  const [subject,setSubject]=useState("");
  const [trial, setTrial] = useState("");
  const [catalog, setCatalog] = useState<ClinicalTrialCatalogItem[]>([]);
  const [provider, setProvider] = useState("fake");
  const [batchId, setBatchId] = useState("");
  const [batches, setBatches] = useState<CdiscPublishedBatch[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<DynamicState | null>(null);

  useEffect(()=>{let alive=true;listClinicalDataSpaces(role).then(items=>{if(alive)setDataSpaces(Array.isArray(items)?items:[])}).catch(()=>{if(alive)setDataSpaces([])});return()=>{alive=false}},[role]);

  // 已发布数据版本（Published data versions）来自受治理发布桥。选中一个版本意味着本次调查被
  // 锁定在该批次：运行时只能读取该版本投影出的记录，缺失的域会被如实标记为能力缺口。
  useEffect(() => {
    let alive = true;
    listPublishedCdiscBatches(role)
      .then((items) => {
        if (alive) setBatches(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (alive) setBatches([]);
      });
    return () => {
      alive = false;
    };
  }, [role]);

  useEffect(() => {
    let alive = true;
    listClinicalTrials(role)
      .then((items) => {
        if (!alive) return;
        const trials = Array.isArray(items) ? items : [];
        setCatalog(trials);
        if (!batchId) setTrial((current) => trials.some((item) => item.trial_id === current) ? current : (trials[0]?.trial_id ?? ""));
      })
      .catch(() => {
        if (alive) setCatalog([]);
      });
    return () => { alive = false; };
  }, [role, batchId]);

  const selected = batches.find((item) => item.batch_id === batchId);
  const trialOptions: ClinicalTrialCatalogItem[] = selected
    ? (selected.trial_ids ?? []).map((trialId) => catalog.find((item) => item.trial_id === trialId) ?? ({ trial_id: trialId, title: "已发布试验" }))
    : catalog;
  const chooseBatch = (value: string) => {
    setBatchId(value);
    const picked = batches.find((item) => item.batch_id === value);
    if (picked?.trial_ids?.[0]) setTrial(picked.trial_ids[0]);
    else setTrial(catalog[0]?.trial_id ?? "");
  };

  const run = async () => {
    setBusy(true);
    setError("");
    try {
      const state = space==='clinical_trial' ? await runDynamicInvestigation({
          question,
          trial_id: trial,
          provider,
          published_batch_id: batchId || undefined,
        },role) : await runPublicClinicalInvestigation({question,subject,space,provider},role);
      setResult(state);
      onCompleted(state);
    } catch (e) {
      setError(e instanceof Error ? e.message : "动态调查失败");
    } finally {
      setBusy(false);
    }
  };
  const boundBatch = String(result?.audit_metadata?.published_batch_id ?? "") || "";
  const boundDomains = Array.isArray(result?.audit_metadata?.available_domains)
    ? (result?.audit_metadata?.available_domains as string[])
    : [];
  const selectedSpace=dataSpaces.find(item=>item.space===space);
  return (
    <section className="case-sheet">
      <header>
        <div>
          <h1>提出临床调查问题</h1>
          <p>系统根据已发布数据域动态选择工具，不套用固定中心、地区或访视。</p>
        </div>
        <Tag type="blue">Runtime V16</Tag>
      </header>
      <div className="composer-grid">
        <label>
          调查空间
          <select aria-label="调查空间" value={space} onChange={(e)=>{setSpace(e.target.value as InvestigationSpace);setResult(null)}}>
            <option value="clinical_trial">受试者级临床试验分析</option>
            {dataSpaces.filter(item=>item.space!=='clinical_trial').map(item=><option key={item.space} value={item.space}>{item.title}</option>)}
          </select>
        </label>
        <label className="question-field">
          临床问题
          <textarea
            aria-label="临床问题"
            rows={4}
            value={question}
            placeholder="例如：为什么本试验的严重不良事件在近期增加？"
            onChange={(e) => setQuestion(e.target.value)}
          />
        </label>
        {space==='clinical_trial' ? <label>
          临床试验
          <select aria-label="临床试验" value={trial} onChange={(e) => setTrial(e.target.value)}>
            {trialOptions.length === 0 && <option value="">当前身份没有可用试验</option>}
            {trialOptions.map((item) => (
              <option key={item.trial_id} value={item.trial_id}>
                {item.title} · {item.trial_id}{item.phase ? ` · ${item.phase}` : ""}
              </option>
            ))}
          </select>
          {trial && (() => {
            const item = trialOptions.find((candidate) => candidate.trial_id === trial);
            return item ? <small>{item.status ? `状态：${item.status}；` : ""}{item.primary_endpoint ? `主要终点：${item.primary_endpoint}` : ""}</small> : null;
          })()}
        </label> : <label>
          调查主题
          <input aria-label="调查主题" value={subject} placeholder="药物、疾病、干预或队列关键词" onChange={(e)=>setSubject(e.target.value)} />
          <small>模型可以规范检索词，但不能跨越所选数据证据类别。</small>
        </label>}
        {space==='clinical_trial' && <label>
          数据版本（已发布批次）
          <select
            aria-label="数据版本（已发布批次）"
            value={batchId}
            onChange={(e) => chooseBatch(e.target.value)}
          >
            <option value="">默认治理数据（Governed default）</option>
            {batches.map((item) => (
              <option key={item.batch_id} value={item.batch_id}>
                {item.batch_id} · {(item.trial_ids ?? []).join(", ")} ·{" "}
                {item.record_count} rows
              </option>
            ))}
          </select>
        </label>}
        <label>
          推理模型
          <select
            aria-label="推理模型"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            <option value="fake">本地确定性演示</option>
            <option value="anthropic">Claude</option>
            <option value="deepseek">DeepSeek</option>
            <option value="openai">OpenAI</option>
            <option value="glm">智谱 GLM</option>
            <option value="kimi">Kimi</option>
            <option value="custom">自定义兼容模型</option>
          </select>
        </label>
      </div>
      {selectedSpace && <aside className="data-space-card"><strong>{selectedSpace.title}</strong><span>{selectedSpace.source} · {selectedSpace.data_reality}</span><p>{selectedSpace.what_it_is}</p><p>可以回答：{selectedSpace.can_answer}</p><p>不能证明：{selectedSpace.cannot_prove}</p>{selectedSpace.inventory&&<p>当前数据库：{Number(selectedSpace.inventory.record_count ?? selectedSpace.inventory.patient_count ?? selectedSpace.inventory.drug_count ?? 0).toLocaleString()} 个核心对象；典型内容：{selectedSpace.inventory.examples?.slice(0,6).map(item=>item.label).join('、')||'等待数据画像'}</p>}{selectedSpace.examples?.length>0&&<small>问题示例：{selectedSpace.examples.join('；')}</small>}</aside>}
      {selected && (
        <p className="composer-bound">
          已锁定数据版本（Bound batch）{selected.batch_id}；域覆盖（Domain coverage）：
          {domains(selected).join("、") || "未提供"}
          。未覆盖的域会被标记为数据缺口，运行时不回退到其他版本。
        </p>
      )}
      {error && (
        <p role="alert" className="clinical-error">
          {error}
        </p>
      )}
      <Button
        onClick={run}
        disabled={busy || (space==='clinical_trial'?!trial:!subject.trim()) || question.trim().length < 5 || role === "viewer"}
      >
        {busy ? "正在调查…" : "开始动态调查"}
      </Button>
      {result && (
        <section className="runtime-result">
          <h2>调查结论</h2>
          {result.report ? <StructuredReport report={result.report} /> : <p>{result.answer}</p>}
          {result.steps.length>0&&<section className="investigation-timeline" aria-label="调查过程">
            <h2>调查过程</h2>
            <p>下面按实际执行顺序说明系统查了哪些地方。</p>
            <ol>{result.steps.map(step=><li key={`${step.sequence}-${step.tool}`}><span>{step.sequence}</span><div><strong>{toolLabels[step.tool]??step.tool}</strong><p>{step.summary??'已完成受治理查询'}</p></div></li>)}</ol>
          </section>}
          {result.hypotheses.length > 0 && <HypothesisReview result={result}/>}
          <EvidenceChain evidence={result.evidence} />
          {boundBatch && (
            <p className="composer-bound">
              本次调查绑定数据版本（Bound batch）{boundBatch}；运行时可用数据域（Available
              domains）：{boundDomains.join("、") || "无"}
              。未列出的域代表该版本未发布，调查不会用其他版本的数据补齐。
            </p>
          )}
        </section>
      )}
    </section>
  );
}

