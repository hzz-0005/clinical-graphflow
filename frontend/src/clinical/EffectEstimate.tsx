import {Tile} from '@carbon/react';

type Props={estimate?:number;interval?:{lower:number;upper:number;level?:number}};

export function EffectEstimate({estimate,interval}:Props){
  if(estimate===undefined||!interval)return <Tile className="clinical-empty-card">Effect estimate unavailable（效应估计不可用）</Tile>;
  const level=Math.round((interval.level??0.95)*100);
  return <Tile className="effect-card" aria-label="Treatment effect（治疗效应）">
    <p>治疗效应（Treatment Effect）</p>
    <strong>{estimate.toFixed(2)}</strong>
    <span>{level}% 置信区间（Confidence Interval）</span>
    <b>{interval.lower.toFixed(2)} – {interval.upper.toFixed(2)}</b>
  </Tile>;
}

