{{ config(
    post_hook=[
        "REVOKE ALL PRIVILEGES ON TABLE {{ this }} FROM insightflow_reader",
        "REVOKE ALL PRIVILEGES ON TABLE {{ this }} FROM PUBLIC"
    ]
) }}

with published_observations as (
    select
        records.batch_id,
        batches.status as batch_status,
        batches.content_hash,
        batches.published_at,
        records.domain_name,
        records.domain_version,
        records.row_key,
        records.source_filename,
        records.payload_json,
        records.created_at
    from {{ source('clinical_ingestion', 'domain_records') }} records
    join {{ source('clinical_ingestion', 'import_batches') }} batches
      on batches.batch_id = records.batch_id
    where batches.status = 'published'
      and records.batch_id like 'public-%'
      and records.domain_name = 'OBSERVATION'
      and batches.quality #>> '{source,source_name}' = 'Synthea'
      and batches.quality #>> '{source,source_class}' = 'synthetic_patient'
), normalized as (
    select
        batch_id,
        batch_status,
        content_hash,
        published_at,
        domain_name,
        domain_version,
        row_key,
        source_filename,
        created_at,
        nullif(trim(coalesce(payload_json->>'PATIENT_ID', payload_json->>'PATIENT')), '') as patient_id,
        nullif(trim(coalesce(payload_json->>'ENCOUNTER_ID', payload_json->>'ENCOUNTER')), '') as encounter_id,
        nullif(trim(coalesce(payload_json->>'OBSERVED_AT', payload_json->>'DATE')), '') as observed_at_text,
        nullif(trim(coalesce(payload_json->>'CODE', payload_json->>'code')), '') as concept_code,
        nullif(trim(coalesce(payload_json->>'DESCRIPTION', payload_json->>'description')), '') as concept_name,
        nullif(trim(coalesce(payload_json->>'VALUE', payload_json->>'value')), '') as value,
        nullif(trim(coalesce(payload_json->>'UNITS', payload_json->>'units')), '') as units
    from published_observations
), validated as (
    select
        normalized.*,
        observed_at_text is not null
        and (
            pg_input_is_valid(observed_at_text, 'timestamptz')
            or (
                observed_at_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                and pg_input_is_valid(observed_at_text, 'date')
            )
        ) as observed_at_is_valid,
        value is not null
        and length(value) <= 128
        and value ~ '^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$'
        and pg_input_is_valid(value, 'numeric') as numeric_is_valid
    from normalized
)
select
    batch_id,
    batch_status,
    content_hash,
    published_at,
    domain_name,
    domain_version,
    row_key,
    source_filename,
    patient_id,
    encounter_id,
    observed_at_text as observed_at_raw,
    case
        when observed_at_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
         and pg_input_is_valid(observed_at_text, 'date')
            then observed_at_text::date::timestamp at time zone 'UTC'
        when observed_at_is_valid
            then observed_at_text::timestamptz
        else null
    end as observed_at,
    case
        when observed_at_text is null then 'missing'
        when observed_at_is_valid then 'valid'
        else 'invalid'
    end as observed_at_parse_status,
    concept_code,
    concept_name,
    value,
    units,
    case when numeric_is_valid then value::numeric else null end as numeric_value,
    case
        when value is null then 'missing'
        when numeric_is_valid then 'numeric'
        else 'non_numeric'
    end as numeric_parse_status,
    created_at
from validated

