-- Read-only Agent access to dbt-managed clinical analytics schemas.
-- This file is idempotent and can also be applied to an existing database.

CREATE SCHEMA IF NOT EXISTS analytics_clinical_staging AUTHORIZATION insightflow;
CREATE SCHEMA IF NOT EXISTS analytics_clinical_core AUTHORIZATION insightflow;
CREATE SCHEMA IF NOT EXISTS analytics_clinical_marts AUTHORIZATION insightflow;

GRANT USAGE ON SCHEMA analytics_clinical_staging TO insightflow_reader;
GRANT USAGE ON SCHEMA analytics_clinical_core TO insightflow_reader;
GRANT USAGE ON SCHEMA analytics_clinical_marts TO insightflow_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA analytics_clinical_staging TO insightflow_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_clinical_core TO insightflow_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_clinical_marts TO insightflow_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA analytics_clinical_staging
GRANT SELECT ON TABLES TO insightflow_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics_clinical_core
GRANT SELECT ON TABLES TO insightflow_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics_clinical_marts
GRANT SELECT ON TABLES TO insightflow_reader;

