\set ON_ERROR_STOP on

BEGIN;

-- V27 exposes longitudinal observations only through this fixed aggregate function.
-- The underlying staging relation contains patient keys/raw values and is deliberately
-- not readable by insightflow_reader (see 017_ehr_observation_acl.sql).
-- Reference ranges are maintained separately from Synthea OBSERVATION rows.  They are
-- versioned, reviewed metadata; an empty table is valid and keeps the aggregate fail-closed
-- until an owner loads an auditable source.  The table contains no patient-level data.
CREATE TABLE IF NOT EXISTS analytics_clinical_core.ehr_reference_ranges (
    catalog_version text NOT NULL CHECK (length(btrim(catalog_version)) BETWEEN 1 AND 40),
    concept_code text NOT NULL CHECK (length(btrim(concept_code)) BETWEEN 1 AND 200),
    unit text NOT NULL CHECK (length(btrim(unit)) BETWEEN 1 AND 80),
    low numeric NOT NULL,
    high numeric NOT NULL,
    boundary_policy text NOT NULL DEFAULT 'inclusive_normal'
        CHECK (boundary_policy = 'inclusive_normal'),
    population_context text NOT NULL DEFAULT 'general'
        CHECK (length(btrim(population_context)) BETWEEN 1 AND 120),
    source text NOT NULL CHECK (length(btrim(source)) BETWEEN 1 AND 500),
    source_uri text,
    source_version text,
    source_content_hash text CHECK (source_content_hash IS NULL OR source_content_hash ~ '^[a-f0-9]{64}$'),
    review_status text NOT NULL CHECK (review_status IN ('draft', 'published', 'withdrawn')),
    effective_from date NOT NULL,
    effective_to date,
    reviewed_by text,
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (catalog_version, concept_code, unit, population_context),
    CHECK (low IS NOT NULL AND high IS NOT NULL AND low <= high),
    CHECK (effective_to IS NULL OR effective_to >= effective_from),
    CHECK ((review_status = 'published' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
        OR review_status <> 'published'),
    CHECK (review_status <> 'published' OR source_uri IS NOT NULL OR source_version IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_ehr_reference_ranges_lookup
    ON analytics_clinical_core.ehr_reference_ranges(concept_code, unit, catalog_version, effective_from);

-- The reader executes the SECURITY DEFINER aggregate but must not insert or alter catalog
-- metadata.  This also prevents a caller from treating an unreviewed row as a medical fact.
REVOKE ALL PRIVILEGES ON TABLE analytics_clinical_core.ehr_reference_ranges FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE analytics_clinical_core.ehr_reference_ranges FROM insightflow_reader;

-- Drop an earlier development signature if one exists: PostgreSQL cannot replace
-- a function when its OUT-column types differ.  This is a definition-only drop;
-- the fixed function and its ACL are recreated below on every apply.
DROP FUNCTION IF EXISTS analytics_clinical_core.get_ehr_observation_snapshot();
CREATE OR REPLACE FUNCTION analytics_clinical_core.get_ehr_observation_snapshot()
RETURNS TABLE (
    batch_id text,
    content_hash text,
    published_at timestamptz,
    record_count bigint,
    manifest jsonb
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT batches.batch_id,
           batches.content_hash,
           batches.published_at,
           batches.record_count::bigint,
           coalesce(batches.quality -> 'source', '{}'::jsonb)
      FROM clinical_ingestion.import_batches batches
     WHERE batches.status = 'published'
       AND batches.batch_id LIKE 'public-%'
       AND batches.quality #>> '{source,source_name}' = 'Synthea'
       AND batches.quality #>> '{source,source_class}' = 'synthetic_patient'
     ORDER BY batches.published_at DESC NULLS LAST, batches.batch_id DESC
     LIMIT 1;
$$;

DROP FUNCTION IF EXISTS analytics_clinical_core.analyze_ehr_observation_trend(
    text, text, text, date, date, text, text, integer
);
CREATE OR REPLACE FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(
    p_cohort_query text,
    p_concept_query text,
    p_time_grain text,
    p_start_date date DEFAULT NULL,
    p_end_date date DEFAULT NULL,
    p_unit text DEFAULT NULL,
    p_reference_catalog_version text DEFAULT NULL,
    p_limit integer DEFAULT 120
)
RETURNS TABLE (
    time_bucket date,
    concept_code text,
    concept_name text,
    unit text,
    observation_count bigint,
    numeric_observation_count bigint,
    patient_count bigint,
    classified_patient_count bigint,
    abnormal_patient_count bigint,
    abnormal_patient_rate numeric,
    reference_range_status text,
    reference_range_source text,
    suppressed boolean,
    suppression_reason text,
    invalid_time_count bigint,
    non_numeric_count bigint,
    reference_missing_count bigint,
    unit_mismatch_count bigint,
    source_batch_id text,
    source_content_hash text,
    source_published_at timestamptz,
    source_record_count bigint,
    source_manifest jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
-- Every application relation is schema-qualified; do not inherit caller schemas.
SET search_path = pg_catalog
AS $$
DECLARE
    v_batch_id text;
    v_content_hash text;
    v_published_at timestamptz;
    v_record_count bigint;
    v_manifest jsonb;
    v_start_date date;
    v_end_date date;
    v_bucket_count integer;
BEGIN
    IF p_cohort_query IS NULL OR length(btrim(p_cohort_query)) = 0 OR length(p_cohort_query) > 200 THEN
        RAISE EXCEPTION 'invalid_cohort_query' USING ERRCODE = '22023';
    END IF;
    IF p_concept_query IS NULL OR length(btrim(p_concept_query)) = 0 OR length(p_concept_query) > 200 THEN
        RAISE EXCEPTION 'invalid_concept_query' USING ERRCODE = '22023';
    END IF;
    IF p_time_grain IS NULL OR p_time_grain NOT IN ('day', 'week', 'month') THEN
        RAISE EXCEPTION 'invalid_time_grain' USING ERRCODE = '22023';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 120 THEN
        RAISE EXCEPTION 'limit_exceeds_120' USING ERRCODE = '22023';
    END IF;
    IF p_start_date IS NOT NULL AND p_end_date IS NOT NULL AND p_end_date < p_start_date THEN
        RAISE EXCEPTION 'invalid_date_window' USING ERRCODE = '22023';
    END IF;
    IF (p_start_date IS NULL) <> (p_end_date IS NULL) THEN
        RAISE EXCEPTION 'window_required' USING ERRCODE = '22023';
    END IF;

    SELECT batches.batch_id,
           batches.content_hash,
           batches.published_at,
           batches.record_count::bigint,
           coalesce(batches.quality -> 'source', '{}'::jsonb)
      INTO v_batch_id, v_content_hash, v_published_at, v_record_count, v_manifest
      FROM clinical_ingestion.import_batches batches
     WHERE batches.status = 'published'
       AND batches.batch_id LIKE 'public-%'
       AND batches.quality #>> '{source,source_name}' = 'Synthea'
       AND batches.quality #>> '{source,source_class}' = 'synthetic_patient'
     ORDER BY batches.published_at DESC NULLS LAST, batches.batch_id DESC
     LIMIT 1;
    IF v_batch_id IS NULL THEN
        RETURN;
    END IF;

    -- An omitted boundary means the complete active Synthea observation window.  Resolve
    -- it inside the function so callers cannot select a batch or silently truncate a window.
    SELECT min((o.observed_at AT TIME ZONE 'UTC')::date),
           max((o.observed_at AT TIME ZONE 'UTC')::date)
      INTO v_start_date, v_end_date
    FROM analytics_clinical_staging.stg_public_ehr_observations o
    WHERE o.batch_id = v_batch_id
      AND o.batch_status = 'published'
      AND o.domain_name = 'OBSERVATION'
      AND o.observed_at_parse_status = 'valid';
    v_start_date := coalesce(p_start_date, v_start_date);
    v_end_date := coalesce(p_end_date, v_end_date);

    IF v_start_date IS NOT NULL AND v_end_date IS NOT NULL THEN
        SELECT count(*)::integer
          INTO v_bucket_count
        FROM generate_series(
            CASE p_time_grain
                WHEN 'day' THEN v_start_date::timestamp
                WHEN 'week' THEN date_trunc('week', v_start_date::timestamp)
                ELSE date_trunc('month', v_start_date::timestamp)
            END,
            CASE p_time_grain
                WHEN 'day' THEN v_end_date::timestamp
                WHEN 'week' THEN date_trunc('week', v_end_date::timestamp)
                ELSE date_trunc('month', v_end_date::timestamp)
            END,
            CASE p_time_grain
                WHEN 'day' THEN interval '1 day'
                WHEN 'week' THEN interval '1 week'
                ELSE interval '1 month'
            END
        ) AS buckets;
        IF v_bucket_count > 120 THEN
            RAISE EXCEPTION 'window_required' USING ERRCODE = '22023';
        END IF;
    END IF;

    RETURN QUERY
    WITH cohort_patients AS (
        SELECT DISTINCT records.batch_id,
                        nullif(trim(records.payload_json->>'PATIENT_ID'), '') AS patient_id
        FROM analytics_clinical_staging.stg_public_domain_records records
        JOIN clinical_ingestion.import_batches batches
          ON batches.batch_id = records.batch_id
        WHERE records.batch_id = v_batch_id
          AND batches.status = 'published'
          AND batches.quality #>> '{source,source_name}' = 'Synthea'
          AND batches.quality #>> '{source,source_class}' = 'synthetic_patient'
          AND records.batch_id LIKE 'public-%'
          AND records.domain_name IN ('CONDITION', 'MEDICATION', 'PROCEDURE', 'OBSERVATION')
          AND records.payload_json::text ILIKE ('%' || replace(replace(replace(coalesce(p_cohort_query, ''), E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_') || '%') ESCAPE E'\\'
          AND nullif(trim(records.payload_json->>'PATIENT_ID'), '') IS NOT NULL
    ), candidate AS (
        SELECT DISTINCT ON (observations.batch_id, observations.row_key)
               observations.batch_id,
               observations.row_key,
               observations.patient_id,
               observations.observed_at,
               observations.observed_at_parse_status,
               coalesce(nullif(trim(observations.concept_code), ''),
                        nullif(trim(observations.concept_name), '')) AS concept_code,
               coalesce(nullif(trim(observations.concept_name), ''),
                        nullif(trim(observations.concept_code), '')) AS concept_name,
               observations.units,
               observations.numeric_value,
               observations.numeric_parse_status
        FROM analytics_clinical_staging.stg_public_ehr_observations observations
        JOIN cohort_patients
          ON cohort_patients.batch_id = observations.batch_id
         AND cohort_patients.patient_id = observations.patient_id
        JOIN clinical_ingestion.import_batches batches
          ON batches.batch_id = observations.batch_id
        WHERE observations.batch_id = v_batch_id
          AND batches.status = 'published'
          AND batches.quality #>> '{source,source_name}' = 'Synthea'
          AND batches.quality #>> '{source,source_class}' = 'synthetic_patient'
          AND observations.batch_id LIKE 'public-%'
          AND observations.batch_status = 'published'
          AND observations.domain_name = 'OBSERVATION'
          AND (
              nullif(trim(observations.concept_code), '') IS NOT NULL
              OR nullif(trim(observations.concept_name), '') IS NOT NULL
          )
          AND (
              nullif(trim(observations.concept_code), '') ILIKE ('%' || replace(replace(replace(coalesce(p_concept_query, ''), E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_') || '%') ESCAPE E'\\'
              OR nullif(trim(observations.concept_name), '') ILIKE ('%' || replace(replace(replace(coalesce(p_concept_query, ''), E'\\', E'\\\\'), '%', E'\\%'), '_', E'\\_') || '%') ESCAPE E'\\'
          )
        ORDER BY observations.batch_id, observations.row_key, observations.created_at DESC NULLS LAST
    ), valid_candidate AS (
        SELECT candidate.*
        FROM candidate
        WHERE candidate.observed_at_parse_status = 'valid'
          AND candidate.observed_at >= (v_start_date::timestamp AT TIME ZONE 'UTC')
          AND candidate.observed_at < ((v_end_date + 1)::timestamp AT TIME ZONE 'UTC')
    ), selected AS (
        SELECT valid_candidate.*
        FROM valid_candidate
        WHERE p_unit IS NULL OR valid_candidate.units = trim(p_unit)
    ), bucketed AS (
        SELECT selected.*,
               CASE p_time_grain
                   WHEN 'day' THEN (selected.observed_at AT TIME ZONE 'UTC')::date
                   WHEN 'week' THEN date_trunc('week', selected.observed_at AT TIME ZONE 'UTC')::date
                   ELSE date_trunc('month', selected.observed_at AT TIME ZONE 'UTC')::date
               END AS bucket
        FROM selected
    ), group_keys AS (
        SELECT DISTINCT bucketed.bucket,
                        bucketed.concept_code,
                        bucketed.concept_name,
                        bucketed.units
        FROM bucketed
    ), reference_candidates AS (
        SELECT g.bucket,
               g.concept_code,
               g.units,
               ranges.catalog_version,
               ranges.low,
               ranges.high,
               ranges.source
        FROM group_keys g
        JOIN analytics_clinical_core.ehr_reference_ranges ranges
          ON ranges.concept_code = g.concept_code
         AND ranges.unit = g.units
         AND ranges.population_context = 'general'
         AND ranges.review_status = 'published'
         AND ranges.effective_from <= g.bucket
         AND (ranges.effective_to IS NULL OR g.bucket <= ranges.effective_to)
         AND (p_reference_catalog_version IS NULL
              OR ranges.catalog_version = btrim(p_reference_catalog_version))
    ), code_catalog AS (
        SELECT g.bucket,
               g.concept_code,
               g.units,
               count(all_ranges.catalog_version)::bigint AS all_code_count,
               count(all_ranges.catalog_version) FILTER (
                   WHERE p_reference_catalog_version IS NOT NULL
                     AND all_ranges.catalog_version = btrim(p_reference_catalog_version)
               )::bigint AS requested_code_count
        FROM group_keys g
        LEFT JOIN analytics_clinical_core.ehr_reference_ranges all_ranges
          ON all_ranges.concept_code = g.concept_code
         AND all_ranges.population_context = 'general'
         AND all_ranges.review_status = 'published'
         AND all_ranges.effective_from <= g.bucket
         AND (all_ranges.effective_to IS NULL OR g.bucket <= all_ranges.effective_to)
        GROUP BY g.bucket, g.concept_code, g.units
    ), reference_resolution AS (
        SELECT g.bucket,
               g.concept_code,
               g.units,
               CASE
                   WHEN count(c.catalog_version) = 0
                        AND p_reference_catalog_version IS NOT NULL
                        AND max(cc.requested_code_count) > 0 THEN 'unit_mismatch'
                   WHEN count(c.catalog_version) = 0
                        AND p_reference_catalog_version IS NOT NULL
                        AND max(cc.all_code_count) > 0 THEN 'unavailable'
                   WHEN count(c.catalog_version) = 0
                        AND p_reference_catalog_version IS NULL
                        AND max(cc.all_code_count) > 0 THEN 'unit_mismatch'
                   WHEN count(c.catalog_version) = 0 THEN 'unknown'
                   WHEN p_reference_catalog_version IS NULL
                        AND count(DISTINCT c.catalog_version) > 1 THEN 'unavailable'
                   WHEN count(DISTINCT (c.low, c.high)) > 1 THEN 'unavailable'
                   ELSE 'available'
               END AS reference_range_status,
               CASE
                   WHEN count(c.catalog_version) > 0
                    AND (p_reference_catalog_version IS NOT NULL
                         OR count(DISTINCT c.catalog_version) = 1)
                    AND count(DISTINCT (c.low, c.high)) = 1
                   THEN min(c.low)
                   ELSE NULL
               END AS low,
               CASE
                   WHEN count(c.catalog_version) > 0
                    AND (p_reference_catalog_version IS NOT NULL
                         OR count(DISTINCT c.catalog_version) = 1)
                    AND count(DISTINCT (c.low, c.high)) = 1
                   THEN max(c.high)
                   ELSE NULL
               END AS high,
               CASE
                   WHEN count(c.catalog_version) > 0
                    AND (p_reference_catalog_version IS NOT NULL
                         OR count(DISTINCT c.catalog_version) = 1)
                    AND count(DISTINCT (c.low, c.high)) = 1
                   THEN min(c.source)
                   ELSE NULL
               END AS source
        FROM group_keys g
        LEFT JOIN reference_candidates c
          ON c.bucket = g.bucket
         AND c.concept_code = g.concept_code
         AND c.units IS NOT DISTINCT FROM g.units
        JOIN code_catalog cc
          ON cc.bucket = g.bucket
         AND cc.concept_code = g.concept_code
         AND cc.units IS NOT DISTINCT FROM g.units
        GROUP BY g.bucket, g.concept_code, g.units
    ), patient_values AS (
        SELECT b.bucket,
               b.concept_code,
               b.concept_name,
               b.units,
               b.patient_id,
               count(*) FILTER (WHERE b.numeric_value IS NOT NULL)::bigint AS numeric_count,
               bool_or(b.numeric_value < rr.low OR b.numeric_value > rr.high) AS is_abnormal
        FROM bucketed b
        JOIN reference_resolution rr
          ON rr.bucket = b.bucket
         AND rr.concept_code = b.concept_code
         AND rr.units IS NOT DISTINCT FROM b.units
        WHERE b.patient_id IS NOT NULL
        GROUP BY b.bucket, b.concept_code, b.concept_name, b.units, b.patient_id
    ), classified AS (
        SELECT pv.bucket,
               pv.concept_code,
               pv.concept_name,
               pv.units,
               count(*) FILTER (
                   WHERE pv.numeric_count > 0 AND rr.reference_range_status = 'available'
               )::bigint AS classified_patient_count,
               count(*) FILTER (
                   WHERE pv.numeric_count > 0
                     AND rr.reference_range_status = 'available'
                     AND pv.is_abnormal
               )::bigint AS abnormal_patient_count
        FROM patient_values pv
        JOIN reference_resolution rr
          ON rr.bucket = pv.bucket
         AND rr.concept_code = pv.concept_code
         AND rr.units IS NOT DISTINCT FROM pv.units
        GROUP BY pv.bucket, pv.concept_code, pv.concept_name, pv.units
    ), grouped AS (
        SELECT bucketed.bucket,
               min(bucketed.concept_code) AS grouped_concept_code,
               min(bucketed.concept_name) AS grouped_concept_name,
               bucketed.units AS grouped_unit,
               count(*)::bigint AS grouped_observation_count,
               count(*) FILTER (WHERE bucketed.numeric_value IS NOT NULL)::bigint AS grouped_numeric_count,
               count(DISTINCT bucketed.patient_id)::bigint AS grouped_patient_count
        FROM bucketed
        GROUP BY bucketed.bucket, bucketed.concept_code, bucketed.concept_name, bucketed.units
    ), metadata AS (
        SELECT (SELECT count(*)::bigint FROM candidate
                 WHERE candidate.observed_at_parse_status IN ('missing', 'invalid')) AS invalid_count,
               (SELECT count(*)::bigint FROM selected
                 WHERE selected.numeric_parse_status = 'non_numeric') AS non_numeric_count,
               (SELECT count(*)::bigint
                  FROM selected s
                  LEFT JOIN reference_resolution rr
                    ON rr.bucket = CASE p_time_grain
                        WHEN 'day' THEN (s.observed_at AT TIME ZONE 'UTC')::date
                        WHEN 'week' THEN date_trunc('week', s.observed_at AT TIME ZONE 'UTC')::date
                        ELSE date_trunc('month', s.observed_at AT TIME ZONE 'UTC')::date
                    END
                   AND rr.concept_code = s.concept_code
                   AND rr.units IS NOT DISTINCT FROM s.units
                 WHERE s.numeric_value IS NOT NULL
                   AND coalesce(rr.reference_range_status, 'unknown') <> 'available') AS reference_missing_count,
               (SELECT count(*)::bigint FROM valid_candidate
                 WHERE p_unit IS NOT NULL AND valid_candidate.units IS DISTINCT FROM trim(p_unit)) AS unit_mismatch_count
    )
    SELECT grouped.bucket,
           grouped.grouped_concept_code,
           grouped.grouped_concept_name,
           grouped.grouped_unit,
           CASE WHEN grouped.grouped_patient_count < 10 THEN NULL ELSE grouped.grouped_observation_count END,
           CASE WHEN grouped.grouped_patient_count < 10 THEN NULL ELSE grouped.grouped_numeric_count END,
           CASE WHEN grouped.grouped_patient_count < 10 THEN NULL ELSE grouped.grouped_patient_count END,
           CASE WHEN grouped.grouped_patient_count < 10 OR classified.classified_patient_count < 10
                THEN NULL ELSE classified.classified_patient_count END,
           CASE WHEN grouped.grouped_patient_count < 10 OR classified.classified_patient_count < 10
                THEN NULL ELSE classified.abnormal_patient_count END,
           CASE WHEN grouped.grouped_patient_count < 10 OR classified.classified_patient_count < 10
                THEN NULL
                WHEN classified.classified_patient_count > 0
                THEN classified.abnormal_patient_count::numeric / classified.classified_patient_count
                ELSE NULL END,
           reference.reference_range_status,
           reference.source,
           (grouped.grouped_patient_count < 10 OR classified.classified_patient_count < 10),
           CASE WHEN grouped.grouped_patient_count < 10 OR classified.classified_patient_count < 10
                THEN 'minimum_cell_size' ELSE NULL END,
           metadata.invalid_count,
           metadata.non_numeric_count,
           metadata.reference_missing_count,
           metadata.unit_mismatch_count,
           v_batch_id,
           v_content_hash,
           v_published_at,
           v_record_count,
           v_manifest
    FROM grouped
    JOIN reference_resolution reference
      ON reference.bucket = grouped.bucket
     AND reference.concept_code = grouped.grouped_concept_code
     AND reference.units IS NOT DISTINCT FROM grouped.grouped_unit
    LEFT JOIN classified
      ON classified.bucket = grouped.bucket
     AND classified.concept_code = grouped.grouped_concept_code
     AND classified.units IS NOT DISTINCT FROM grouped.grouped_unit
    CROSS JOIN metadata
    ORDER BY grouped.bucket, grouped.grouped_concept_name NULLS LAST, grouped.grouped_concept_code, grouped.grouped_unit NULLS FIRST
    LIMIT p_limit;
END;
$$;

ALTER FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(text, text, text, date, date, text, text, integer)
    OWNER TO insightflow;
REVOKE ALL PRIVILEGES ON FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(text, text, text, date, date, text, text, integer) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(text, text, text, date, date, text, text, integer) FROM insightflow_reader;
GRANT EXECUTE ON FUNCTION analytics_clinical_core.analyze_ehr_observation_trend(text, text, text, date, date, text, text, integer) TO insightflow_reader;

ALTER FUNCTION analytics_clinical_core.get_ehr_observation_snapshot() OWNER TO insightflow;
REVOKE ALL PRIVILEGES ON FUNCTION analytics_clinical_core.get_ehr_observation_snapshot() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION analytics_clinical_core.get_ehr_observation_snapshot() FROM insightflow_reader;
GRANT EXECUTE ON FUNCTION analytics_clinical_core.get_ehr_observation_snapshot() TO insightflow_reader;

COMMIT;

