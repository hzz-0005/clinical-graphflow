select * from {{ source('clinical_raw', 'participants') }}

