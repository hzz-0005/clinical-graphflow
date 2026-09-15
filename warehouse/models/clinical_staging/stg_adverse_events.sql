select * from {{ source('clinical_raw', 'adverse_events') }}

