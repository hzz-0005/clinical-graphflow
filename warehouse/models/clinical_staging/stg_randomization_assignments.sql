select * from {{ source('clinical_raw', 'randomization_assignments') }}

