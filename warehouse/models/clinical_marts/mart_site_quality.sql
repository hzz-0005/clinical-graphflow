with population as (
    select trial_id, site_id, region, arm, count(*) as participant_count
    from {{ ref('dim_participants') }}
    group by 1, 2, 3, 4
), deviations as (
    select
        trial_id,
        site_id,
        arm,
        count(distinct participant_id) filter (where severity in ('major', 'critical')) as major_deviation_participants
    from {{ ref('fct_protocol_deviations') }}
    group by 1, 2, 3
), handling as (
    select
        site_id,
        count(*) filter (where excursion_flag) as temperature_excursions,
        max(temperature_c) filter (where excursion_flag) as maximum_excursion_temperature_c
    from {{ ref('stg_specimen_handling_events') }}
    group by 1
)
select
    p.trial_id,
    p.site_id,
    p.region,
    p.arm,
    p.participant_count,
    coalesce(d.major_deviation_participants, 0) as major_deviation_participants,
    coalesce(h.temperature_excursions, 0) as temperature_excursions,
    h.maximum_excursion_temperature_c
from population p
left join deviations d using (trial_id, site_id, arm)
left join handling h using (site_id)

