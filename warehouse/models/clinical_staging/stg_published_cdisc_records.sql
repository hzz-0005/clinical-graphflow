with ranked as (
    select
        r.*,
        b.published_at,
        row_number() over (partition by r.participant_key order by b.published_at desc, b.batch_id desc) as publication_rank
    from {{ source('clinical_ingestion', 'canonical_records') }} r
    join {{ source('clinical_ingestion', 'import_batches') }} b using (batch_id)
    where b.status = 'published'
)
select
    participant_key as participant_id,
    batch_id as source_batch_id,
    trial_id,
    site_id,
    arm,
    region,
    intention_to_treat,
    safety_population,
    per_protocol,
    baseline_value as baseline_score,
    week12_value as week12_outcome_score,
    week12_improvement as week12_improvement_score
from ranked
where publication_rank = 1

