import {useEffect,useState} from 'react';
import {InlineLoading,InlineNotification,Tag} from '@carbon/react';
import type {ClinicalCatalogSnapshot,ClinicalTrialCatalogItem,Role} from '../types';
import {getClinicalCatalog,listClinicalTrials} from '../api';
import './clinical.css';

function roleLabel(role:string){return role==='measure'?'measure（指标）':role==='identifier'?'identifier（标识）':role==='dimension'?'dimension（维度）':'unknown（待确认）'}

export function DataCatalogPanel({role}:{role:Role}){
 const [trials,setTrials]=useState<ClinicalTrialCatalogItem[]>([]);
 const [trialId,setTrialId]=useState('');
 const [catalog,setCatalog]=useState<ClinicalCatalogSnapshot|null>(null);
 const [error,setError]=useState('');
 useEffect(()=>{setError('');listClinicalTrials(role).then(items=>{setTrials(items);setTrialId(current=>current||items[0]?.trial_id||'')}).catch(e=>setError(e instanceof Error?e.message:'无法加载试验目录'))},[role]);
 useEffect(()=>{if(!trialId)return;setError('');getClinicalCatalog(trialId,role).then(setCatalog).catch(e=>setError(e instanceof Error?e.message:'无法加载真实数据目录'))},[role,trialId]);
 return <section className="catalog-workspace">
  <header className="catalog-head"><div><p className="kicker">真实数据发现（Runtime Data Discovery）</p><h1>数据目录（Data Catalog）</h1><p>目录来自当前数据库和已发布数据版本的实际字段，不展示患者行或原始内容；它会告诉调查规划器哪些指标和维度确实可用。</p></div><Tag type="teal">V15 · metadata only（仅元数据）</Tag></header>
  {trials.length>0&&<label className="catalog-trial">研究（Trial）<select aria-label="选择研究（Trial）" value={trialId} onChange={e=>setTrialId(e.target.value)}>{trials.map(item=><option key={item.trial_id} value={item.trial_id}>{item.trial_id} · {item.title}</option>)}</select></label>}
  {error&&<InlineNotification kind="error" lowContrast hideCloseButton title="目录加载失败" subtitle={error}/>} {!error&&!catalog&&<InlineLoading description="正在读取真实数据目录…"/>}
  {catalog&&<>
   <div className="catalog-summary"><div><span>已发现数据域</span><strong>{catalog.published_domains.length}</strong></div><div><span>候选指标</span><strong>{catalog.measures.length}</strong></div><div><span>候选维度</span><strong>{catalog.dimensions.length}</strong></div><div><span>数据集</span><strong>{catalog.datasets.length}</strong></div></div>
   <div className="catalog-sets">{catalog.datasets.map(dataset=><article className="catalog-dataset" key={`${dataset.source}:${dataset.domain}`}><header><div><Tag size="sm" type={dataset.is_registered?'green':'warm-gray'}>{dataset.domain}</Tag><h2>{dataset.dataset}</h2></div><strong>{dataset.record_count??'—'} rows</strong></header><p className="catalog-source">{dataset.domain_status==='registered'?'已注册标准域（Registered）':'自定义候选域（Custom candidate）'} · {dataset.grain??'粒度未声明'} · {dataset.source}</p><div className="catalog-fields">{dataset.fields.map(field=><div key={field.name}><code>{field.name}</code><span>{field.inferred_type} · {roleLabel(field.role)}</span>{field.aliases&&field.aliases.length>0&&<small>中文/别名：{field.aliases.join('、')}</small>}</div>)}</div></article>)}</div>
  </>}
 </section>
}

