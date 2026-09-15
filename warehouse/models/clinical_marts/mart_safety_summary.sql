with population as (
    select trial_id, arm, count(*) as randomized_participants
    from {{ ref('dim_participants') }}
    group by 1, 2
), exposed as (
    select trial_id, arm, count(distinct participant_id) as exposed_participants
    from {{ ref('fct_exposures') }}
    where actual_dose > 0
    group by 1, 2
), events as (
    select
        trial_id,
        arm,
        count(distinct participant_id) as participants_with_adverse_event,
        count(distinct participant_id) filter (where serious) as participants_with_serious_adverse_event
    from {{ ref('fct_adverse_events') }}
    group by 1, 2
)
select
    p.trial_id,
    p.arm,
    p.randomized_participants,
    coalesce(x.exposed_participants, 0) as exposed_participants,
    coalesce(e.participants_with_adverse_event, 0) as participants_with_adverse_event,
    coalesce(e.participants_with_serious_adverse_event, 0) as participants_with_serious_adverse_event
from population p
left join exposed x using (trial_id, arm)
left join events e using (trial_id, arm)

