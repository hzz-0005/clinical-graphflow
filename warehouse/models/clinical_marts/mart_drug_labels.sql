select payload_json->>'LABEL_ID' label_id,payload_json->>'SET_ID' set_id,payload_json->>'EFFECTIVE_TIME' effective_time,
       payload_json->>'BRAND_NAMES' brand_names,payload_json->>'GENERIC_NAMES' generic_names,
       payload_json->>'MANUFACTURER_NAMES' manufacturer_names,payload_json->>'PRODUCT_TYPES' product_types,
       payload_json->>'ROUTES' routes,payload_json->>'INDICATIONS_AND_USAGE' indications_and_usage,
       payload_json->>'CONTRAINDICATIONS' contraindications,payload_json->>'BOXED_WARNING' boxed_warning,
       payload_json->>'WARNINGS' warnings,payload_json->>'ADVERSE_REACTIONS' adverse_reactions
from {{ ref('stg_public_domain_records') }} where domain_name='DRUG_LABEL'

