select batch_id, domain_name, domain_version, row_key, source_filename, payload_json, created_at
from {{ source('clinical_ingestion', 'domain_records') }}
where batch_id like 'public-%'

