select * from {{ source('clinical_raw', 'outcome_assessments') }}

