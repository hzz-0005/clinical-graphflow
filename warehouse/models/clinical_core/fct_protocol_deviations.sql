select
    d.*,
    p.trial_id,
    p.region,
    p.arm
from {{ ref('stg_protocol_deviations') }} d
join {{ ref('dim_participants') }} p using (participant_id)

