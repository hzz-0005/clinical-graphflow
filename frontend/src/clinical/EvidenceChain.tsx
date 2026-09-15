import type {Evidence,Role} from '../types';

const hidden=/participant|subject|usubjid/i;
const label:Record<string,string>={arm:'分组',sample_size:'样本量',mean_improvement:'平均改善',site_id:'研究中心',region:'地区',effect:'效应值',value:'数值',report_count:'报告数',serious_report_count:'严重报告数',medicinal_product:'药品原名',medicinal_product_canonical:'标准药品名',medicinal_product_zh:'药品中文名',reaction_term:'不良事件原名',reaction_term_zh:'不良事件中文名',study_count:'研究数',average_enrollment:'平均入组人数',patient_count:'患者数',phase:'研究阶段',overall_status:'研究状态'};
const valueLabel:Record<string,string>={control:'对照组',treatment:'治疗组'};
const displayRows=(rows:Evidence['rows'])=>(rows??[]).map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!hidden.test(key))));
const signalLabel:Record<string,string>={trial_available:'试验总体',treatment_effect:'治疗效应',site_ranking:'中心排名',protocol_quality:'方案与处理质量',exposure:'治疗暴露',missingness:'结局缺失',safety_trend:'安全性趋势',visit_windows:'访视窗口偏离',no_data:'无可用数据'};
const chartPriority=['mean_improvement','effect','value','report_count','study_count','patient_count','sample_size'];

function shown(value:unknown){if(value===null||value===undefined||value==='')return '暂无';if(typeof value==='number')return Number.isInteger(value)?String(value):String(Math.round(value*100)/100);return valueLabel[String(value)]??String(value)}
function isLongText(rows:Array<Record<string,unknown>>){return rows.some(row=>Object.values(row).some(value=>typeof value==='string'&&value.length>240))}
function explanation(item:Evidence){
 const scope=item.sample_size?`结论基于 ${item.sample_size} 条汇总观察。`:'当前响应没有提供可核对的样本量。';
 const gap=item.observation_signal==='no_data'?'本次查询没有返回可用数据，因此它既不支持也不反驳当前假设，只是标记该数据域未覆盖。':'';
 return `这条证据显示：${item.claim}。${scope}${gap}它用于支持或排除调查假设，但单条观察不能独立证明因果。`;
}

function NumericEvidence({rows}:{rows:Array<Record<string,unknown>>}){
 const numericKeys=chartPriority.filter(key=>rows.some(row=>typeof row[key]==='number'));
 const metric=numericKeys[0];if(!metric)return null;
 const category=Object.keys(rows[0]??{}).find(key=>!numericKeys.includes(key)&&rows.every(row=>typeof row[key]==='string'));
 const values=rows.map(row=>Number(row[metric]??0));const maximum=Math.max(...values.map(Math.abs),1);
 if(rows.length>1&&category)return <div className="evidence-bars" role="img" aria-label={`${label[metric]??metric}对比图`}><div className="chart-title"><span>{label[metric]??metric}</span><small>按{label[category]??category}比较</small></div>{rows.slice(0,8).map((row,index)=><div className="bar-row" key={index}><span>{shown(row[category])}</span><div><i style={{width:`${Math.max(3,Math.abs(Number(row[metric]??0))/maximum*100)}%`}}/></div><strong>{shown(row[metric])}</strong></div>)}</div>;
 return <div className="metric-strip">{numericKeys.slice(0,4).map(key=><div key={key}><span>{label[key]??key}</span><strong>{shown(rows[0][key])}</strong></div>)}</div>;
}

function EvidencePreview({item}:{item:Evidence}){
 const rows=displayRows(item.rows);
 if(!rows.length)return <p className="empty-evidence">汇总样本量：{item.sample_size??'未提供'}</p>;
 if(isLongText(rows))return <div className="text-evidence-note"><strong>监管文本已整理为上方结论</strong><span>完整原文保留在技术详情中，避免大段数据库字段遮挡关键结论。</span></div>;
 if(chartPriority.some(key=>rows.some(row=>typeof row[key]==='number')))return <NumericEvidence rows={rows}/>;
 const columns=Array.from(new Set(rows.flatMap(row=>Object.keys(row)))).slice(0,6);
 return <div className="evidence-table-wrap"><table className="evidence-table"><thead><tr>{columns.map(key=><th key={key}>{label[key]??key}</th>)}</tr></thead><tbody>{rows.slice(0,10).map((row,index)=><tr key={index}>{columns.map(key=><td key={key}>{shown(row[key])}</td>)}</tr>)}</tbody></table></div>;
}

export function EvidenceChain({evidence,role='analyst'}:{evidence:Evidence[];role?:Role}){
 return <section className="evidence-chain" aria-label="证据链"><header><div><h2>证据链</h2><p>先看结论和图表，需要复核时再展开原始数据。</p></div><span>{evidence.length} 项证据</span></header>{evidence.map((item,index)=><article className="evidence-card" key={item.evidence_id}><div className="evidence-index"><span>{String(index+1).padStart(2,'0')}</span><b>{item.evidence_id}</b></div><div className="evidence-result"><h3><span>证据摘要</span><small>数据库结果</small></h3><strong>{item.claim}</strong>{item.observation_signal&&<p className="evidence-signal">观测信号（Signal）：{signalLabel[item.observation_signal]??item.observation_signal}</p>}<EvidencePreview item={item}/><small>{item.source}</small></div><div className="evidence-meaning"><h3>这说明什么</h3><p>{explanation(item)}</p><p className="evidence-attribution">支持假设（Supports）：{item.supports?.length?item.supports.join('、'):'无'}　反驳假设（Contradicts）：{item.contradicts?.length?item.contradicts.join('、'):'无'}</p><span className="evidence-caveat">解释边界：相关证据，不自动等同于因果结论</span></div>{role!=='viewer'&&<details className="query-detail"><summary><span>查看原始数据与查询</span><small>查看查询详情</small></summary><div className="raw-result"><pre>{JSON.stringify(displayRows(item.rows),null,2)}</pre><pre>{item.sql}</pre></div></details>}</article>)}</section>
}

