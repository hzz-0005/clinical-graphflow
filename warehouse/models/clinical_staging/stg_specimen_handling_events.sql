select * from {{ source('clinical_raw', 'specimen_handling_events') }}

