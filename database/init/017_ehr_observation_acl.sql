\set ON_ERROR_STOP on

-- The longitudinal staging relation contains patient keys and raw observation values.
-- Keep the legacy public staging relation untouched; only this relation loses direct
-- reader access. A future fixed SQL / SECURITY DEFINER adapter can run as its owner.
DO $$
BEGIN
    IF to_regclass('analytics_clinical_staging.stg_public_ehr_observations') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'insightflow_reader') THEN
        REVOKE ALL PRIVILEGES
            ON TABLE analytics_clinical_staging.stg_public_ehr_observations
            FROM insightflow_reader;
        REVOKE ALL PRIVILEGES
            ON TABLE analytics_clinical_staging.stg_public_ehr_observations
            FROM PUBLIC;
    END IF;
END
$$;

