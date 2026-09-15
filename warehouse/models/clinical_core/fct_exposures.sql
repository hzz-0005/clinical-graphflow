select
    e.*,
    p.trial_id,
    p.site_id,
    p.region,
    p.arm
from {{ ref('stg_treatment_exposures') }} e
join {{ ref('dim_participants') }} p using (participant_id)

