select
    o.assessment_id,
    o.participant_id,
    p.trial_id,
    p.site_id,
    p.region,
    p.arm,
    o.visit_week,
    o.scheduled_date,
    o.assessed_at,
    o.outcome_score,
    p.baseline_score,
    case when o.outcome_score is not null then o.outcome_score - p.baseline_score end as improvement_score,
    o.assessment_status,
    o.missing_reason
from {{ ref('stg_outcome_assessments') }} o
join {{ ref('dim_participants') }} p using (participant_id)

