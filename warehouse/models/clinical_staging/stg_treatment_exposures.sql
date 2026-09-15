select * from {{ source('clinical_raw', 'treatment_exposures') }}

