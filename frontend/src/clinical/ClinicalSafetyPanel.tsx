import {InlineLoading,InlineNotification,Tab,TabList,TabPanel,TabPanels,Tabs,Tag} from '@carbon/react';
import type {Investigation,Role} from '../types';
import {EffectEstimate} from './EffectEstimate';
import {GuardrailChecklist} from './GuardrailChecklist';
import {EvidenceChain} from './EvidenceChain';
import './clinical.css';

type Props={investigation?:Investigation|null;role:Role;loading?:boolean;error?:string};
const text=(value:unknown,fallback='Not specified（未指定）')=>typeof value==='string'&&value?value:fallback;
const populationLabel=(value:unknown)=>value==='intention_to_treat'?'Intention-to-Treat（意向治疗集）':text(value,'Intention-to-Treat（意向治疗集）');

export function ClinicalSafetyPanel({investigation,role,loading=false,error}:Props){
  if(loading)return <section className="clinical-state"><InlineLoading description="Loading clinical investigation（正在加载临床调查）"/></section>;
  if(error)return <section className="clinical-state"><InlineNotification kind="error" title="Clinical investigation failed（临床调查加载失败）" subtitle={error} lowContrast/></section>;
  if(!investigation)return <section className="clinical-state"><h1>No clinical investigation（暂无临床调查）</h1><p>Run an aggregate trial question to begin（请先发起总体试验问题）。</p></section>;
  const state=investigation.state;
  const meta=state.audit_metadata??{};
  const verification=state.verification;
  const inconclusive=state.status==='inconclusive'||verification?.status==='inconclusive'||verification?.effect_estimate===undefined;
  const warnings=[...(state.warnings??[]),...(verification?.warnings??verification?.flags??[])];
  const interval=verification?.confidence_interval??(verification?.ci_lower!==undefined&&verification?.ci_upper!==undefined?{lower:verification.ci_lower,upper:verification.ci_upper,level:.95}:undefined);
  return <section className="clinical-panel">
    <header className="clinical-head"><div><p className="kicker">临床试验调查（Clinical Trial Investigation）</p><h1>{state.question}</h1><div className="clinical-tags"><Tag type="blue">{populationLabel(meta.population)}</Tag><Tag type="cyan">{text(meta.subgroup)}</Tag>{typeof meta.batch_id==='string'&&meta.batch_id&&<Tag type="purple">数据版本（Data version） {meta.batch_id}</Tag>}</div></div><Tag type={state.status==='inconclusive'?'red':'warm-gray'}>{state.status==='inconclusive'?'调查状态（Inconclusive）':'待审批（Pending approval）'}</Tag></header>
    <InlineNotification className="research-notice" kind="warning" hideCloseButton title="Research use only（仅供研究使用）" subtitle={investigation.research_use_only??state.research_notice??'Not medical advice; human review is mandatory（非医疗建议，必须人工审核）。'} lowContrast/>
    {warnings.map(w=><InlineNotification key={w} kind="warning" hideCloseButton title="Statistical warning（统计警告）" subtitle={w} lowContrast/>)}
    {inconclusive&&<p className="inconclusive">Inconclusive result（证据不足）</p>}
    <div className="clinical-grid"><div><EffectEstimate estimate={verification?.effect_estimate} interval={interval}/><GuardrailChecklist checks={verification?.checks}/></div><article className="clinical-summary"><p>终点 / 人群（Endpoint / Population）</p><h2>主要疗效终点（Primary efficacy endpoint）</h2><p>{populationLabel(meta.population)} · {text(meta.subgroup)}</p><hr/><p>已验证结论（Verified conclusion）</p><strong>{state.answer??'等待汇总（Awaiting synthesis）'}</strong></article></div>
    <EvidenceChain evidence={state.evidence} role={role}/>
  </section>;
}

