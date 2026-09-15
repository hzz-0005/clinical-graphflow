with records as (select * from {{ ref('stg_public_domain_records') }}), patients as (
 select payload_json->>'PATIENT_ID' patient_id,payload_json->>'BIRTH_DATE' birth_date,payload_json->>'GENDER' gender,
        payload_json->>'RACE' race,payload_json->>'ETHNICITY' ethnicity from records where domain_name='PATIENT'
), counts as (
 select payload_json->>'PATIENT_ID' patient_id,
        count(*) filter(where domain_name='ENCOUNTER') encounter_count,
        count(*) filter(where domain_name='CONDITION') condition_count,
        count(*) filter(where domain_name='MEDICATION') medication_count,
        count(*) filter(where domain_name='PROCEDURE') procedure_count,
        count(*) filter(where domain_name='OBSERVATION') observation_count,
        count(*) filter(where domain_name='DEVICE') device_count
 from records where domain_name in ('ENCOUNTER','CONDITION','MEDICATION','PROCEDURE','OBSERVATION','DEVICE') group by 1
)
select p.*,coalesce(c.encounter_count,0) encounter_count,coalesce(c.condition_count,0) condition_count,
       coalesce(c.medication_count,0) medication_count,coalesce(c.procedure_count,0) procedure_count,
       coalesce(c.observation_count,0) observation_count,coalesce(c.device_count,0) device_count
from patients p left join counts c using(patient_id)

