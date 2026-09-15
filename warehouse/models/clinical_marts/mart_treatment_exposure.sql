select
    p.participant_id,
    p.trial_id,
    p.site_id,
    p.region,
    p.arm,
    sum(e.actual_dose) as actual_dose,
    sum(e.planned_dose) as planned_dose,
    sum(e.actual_dose) / nullif(sum(e.planned_dose), 0) as adherence_rate,
    count(*) filter (where e.adherence_status = 'missed') as missed_doses
from {{ ref('dim_participants') }} p
join {{ ref('fct_exposures') }} e using (participant_id)
group by 1, 2, 3, 4, 5

