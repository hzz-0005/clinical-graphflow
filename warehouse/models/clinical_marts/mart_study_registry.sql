with records as (select * from {{ ref('stg_public_domain_records') }}),
ranked_studies as (
  select r.*,
         row_number() over (
           partition by nullif(payload_json->>'STUDY_ID','')
           order by case when batch_id='public-clinicaltrials' then 0 else 1 end,
                    batch_id,
                    row_key
         ) as source_rank
  from records r
  where domain_name='STUDY' and nullif(payload_json->>'STUDY_ID','') is not null
), studies as (
  select payload_json->>'STUDY_ID' as study_id,payload_json->>'BRIEF_TITLE' as brief_title,
         payload_json->>'STUDY_TYPE' as study_type,payload_json->>'OVERALL_STATUS' as overall_status,
         payload_json->>'PHASES' as phases,nullif(payload_json->>'ENROLLMENT_COUNT','')::integer as enrollment_count,
         payload_json->>'START_DATE' as start_date,payload_json->>'COMPLETION_DATE' as completion_date,
         payload_json->>'LEAD_SPONSOR' as lead_sponsor,payload_json->>'CONDITIONS' as conditions
  from ranked_studies where source_rank=1
), ranked_interventions as (
  select r.*,
         row_number() over (
           partition by nullif(payload_json->>'STUDY_ID',''),
                        coalesce(nullif(payload_json->>'INTERVENTION_ID',''), row_key)
           order by case when batch_id='public-clinicaltrials' then 0 else 1 end,
                    batch_id,
                    row_key
         ) as source_rank
  from records r
  where domain_name='INTERVENTION'
    and nullif(payload_json->>'STUDY_ID','') is not null
), interventions as (
  select payload_json->>'STUDY_ID' as study_id,count(*) as intervention_count,
         string_agg(distinct payload_json->>'INTERVENTION_TYPE',', ' order by payload_json->>'INTERVENTION_TYPE') as intervention_types,
         string_agg(distinct payload_json->>'INTERVENTION_NAME',' | ' order by payload_json->>'INTERVENTION_NAME') as intervention_names
  from ranked_interventions where source_rank=1 group by 1
), ranked_outcomes as (
  select r.*,
         row_number() over (
           partition by nullif(payload_json->>'STUDY_ID',''),
                        coalesce(nullif(payload_json->>'OUTCOME_ID',''), row_key)
           order by case when batch_id='public-clinicaltrials' then 0 else 1 end,
                    batch_id,
                    row_key
         ) as source_rank
  from records r
  where domain_name='OUTCOME'
    and nullif(payload_json->>'STUDY_ID','') is not null
), outcomes as (
  select payload_json->>'STUDY_ID' as study_id,count(*) as outcome_count,
         string_agg(payload_json->>'MEASURE',' | ' order by payload_json->>'OUTCOME_ID') filter(where payload_json->>'OUTCOME_TYPE'='PRIMARY') as primary_outcomes
  from ranked_outcomes where source_rank=1 group by 1
)
select s.*,coalesce(i.intervention_count,0) as intervention_count,i.intervention_types,i.intervention_names,
       coalesce(o.outcome_count,0) as outcome_count,o.primary_outcomes
from studies s left join interventions i using(study_id) left join outcomes o using(study_id)

