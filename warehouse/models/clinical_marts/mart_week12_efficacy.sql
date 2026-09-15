select
    p.participant_id,
    null::text as source_batch_id,
    p.trial_id,
    p.site_id,
    p.region,
    p.arm,
    p.age_band,
    p.sex,
    p.baseline_score,
    p.severity_band,
    p.disease_duration_months,
    o.outcome_score as week12_outcome_score,
    o.improvement_score as week12_improvement_score,
    o.assessment_status,
    o.missing_reason
from {{ ref('dim_participants') }} p
left join {{ ref('fct_outcomes') }} o
  on o.participant_id = p.participant_id
 and o.visit_week = 12
union all
select
    participant_id,
    source_batch_id,
    trial_id,
    site_id,
    region,
    arm,
    null::text as age_band,
    null::text as sex,
    baseline_score,
    null::text as severity_band,
    null::integer as disease_duration_months,
    week12_outcome_score,
    week12_improvement_score,
    case when week12_outcome_score is null then 'missing' else 'completed' end as assessment_status,
    case when week12_outcome_score is null then 'not_provided' else null end as missing_reason
from {{ ref('stg_published_cdisc_records') }}

