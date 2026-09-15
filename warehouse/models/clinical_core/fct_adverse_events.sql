select
    a.*,
    p.trial_id,
    p.site_id,
    p.region,
    p.arm
from {{ ref('stg_adverse_events') }} a
join {{ ref('dim_participants') }} p using (participant_id)

