select * from {{ source('clinical_raw', 'baseline_assessments') }}

