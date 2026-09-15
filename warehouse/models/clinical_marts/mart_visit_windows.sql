select
    p.trial_id,
    p.site_id,
    p.region,
    p.arm,
    v.visit_week,
    count(*) as participant_count,
    count(*) filter (where v.visit_status = 'missed') as missed_visits,
    count(*) filter (where abs(v.window_deviation_days) > 7) as outside_window_visits,
    avg(abs(v.window_deviation_days)) filter (where v.visit_status = 'completed') as mean_absolute_deviation_days
from {{ ref('stg_participant_visits') }} v
join {{ ref('dim_participants') }} p using (participant_id)
group by 1, 2, 3, 4, 5

