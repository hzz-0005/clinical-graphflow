select * from {{ source('clinical_raw', 'protocol_deviations') }}

