select * from {{ source('clinical_raw', 'participant_visits') }}

