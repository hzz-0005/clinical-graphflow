import {CheckmarkFilled,WarningFilled} from '@carbon/icons-react';
import type {ClinicalCheck} from '../types';

export function GuardrailChecklist({checks=[]}:{checks?:ClinicalCheck[]}){
  return <section className="guardrails" aria-label="Safety guardrails（安全护栏）">
    <h2>安全护栏（Safety Guardrails）</h2>
    {checks.length===0?<p>No verification results（暂无验证结果）</p>:checks.map(check=><article key={check.name} className={check.passed?'passed':'warning'}>
      {check.passed?<CheckmarkFilled size={20}/>:<WarningFilled size={20}/>}<div><strong>{check.name}</strong><p>{check.message??check.detail}</p></div>
    </article>)}
  </section>;
}

