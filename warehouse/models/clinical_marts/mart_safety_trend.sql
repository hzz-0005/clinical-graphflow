with population as (
    select trial_id, arm, count(*) as participant_count
    from {{ ref('dim_participants') }}
    group by 1, 2
), events as (
    select
        trial_id,
        region,
        arm,
        date_trunc('month', started_at)::date as event_month,
        count(*) as adverse_event_count,
        count(*) filter (where serious) as serious_event_count,
        count(distinct participant_id) as participants_with_event
    from {{ ref('fct_adverse_events') }}
    group by 1, 2, 3, 4
)
select
    e.*,
    p.participant_count,
    e.participants_with_event::numeric / nullif(p.participant_count, 0) as participant_event_rate
from events e
join population p using (trial_id, arm)

