select
    participant_id,
    source_batch_id,
    trial_id,
    site_id,
    region,
    arm,
    assessment_status,
    missing_reason,
    assessment_status = 'missing' as week12_missing
from {{ ref('mart_week12_efficacy') }}

