select
    participant_id,
    null::text as source_batch_id,
    trial_id,
    site_id,
    region,
    arm,
    age_band,
    sex,
    baseline_score,
    severity_band,
    disease_duration_months
from {{ ref('dim_participants') }}
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
    null::integer as disease_duration_months
from {{ ref('stg_published_cdisc_records') }}

