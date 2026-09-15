select
    p.participant_id,
    p.trial_id,
    p.site_id,
    p.region,
    p.age_band,
    p.sex,
    p.enrolled_at,
    p.discontinued_at,
    p.discontinuation_reason,
    a.arm,
    a.randomized_at,
    a.stratification_region,
    a.randomization_block,
    b.baseline_score,
    b.severity_band,
    b.disease_duration_months
from {{ ref('stg_participants') }} p
join {{ ref('stg_randomization_assignments') }} a using (participant_id, trial_id)
join {{ ref('stg_baseline_assessments') }} b using (participant_id)

