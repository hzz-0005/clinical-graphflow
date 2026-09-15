param(
    [string]$ComposeService = "postgres",
    [string]$DatabaseUser = "insightflow",
    [string]$DatabaseName = "insightflow"
)

$ErrorActionPreference = "Stop"

function Step([string]$Title) {
    Write-Host "`n==> $Title" -ForegroundColor Cyan
}

function Assert-ExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Assert-True([string]$Query, [string]$Message) {
    $result = docker compose exec -T $ComposeService psql -At -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName -c $Query
    Assert-ExitCode "V27 structure check failed: $Query"
    if (($result | Out-String).Trim() -ne "t") { throw $Message }
}

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Push-Location $repoRoot
try {
    Step "1/3 wait for PostgreSQL"
    docker compose up -d --wait $ComposeService
    Assert-ExitCode "PostgreSQL did not become healthy"
    docker compose exec -T $ComposeService psql -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName -c "SELECT 1" | Out-Host
    Assert-ExitCode "PostgreSQL connection check failed"

    function Invoke-Migration([string]$MigrationFile) {
        $path = Join-Path $repoRoot "database\init\$MigrationFile"
        if (-not (Test-Path -LiteralPath $path)) { throw "Migration file not found: $path" }
        Step "apply $MigrationFile"
        # ON_ERROR_STOP stops on the first statement failure; this script does not
        # attempt destructive rollback.
        $sql = Get-Content -Raw -LiteralPath $path
        $sql | docker compose exec -T $ComposeService psql -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName
        Assert-ExitCode "$MigrationFile failed; stopped without destructive rollback"
    }

    Invoke-Migration "017_ehr_observation_acl.sql"
    Invoke-Migration "018_ehr_observation_trend.sql"

    Step "3/3 verify V27 function, reference catalog and ACL"
    $function = "analytics_clinical_core.analyze_ehr_observation_trend(text,text,text,date,date,text,text,integer)"
    $snapshotFunction = "analytics_clinical_core.get_ehr_observation_snapshot()"
    Assert-True "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='analytics_clinical_core' AND p.proname='analyze_ehr_observation_trend' AND pg_get_function_identity_arguments(p.oid)='p_cohort_query text, p_concept_query text, p_time_grain text, p_start_date date, p_end_date date, p_unit text, p_reference_catalog_version text, p_limit integer')" "V27 fixed function is missing or has the wrong signature"
    Assert-True "SELECT p.prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='analytics_clinical_core' AND p.proname='analyze_ehr_observation_trend'" "V27 function is not SECURITY DEFINER"
    Assert-True "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='analytics_clinical_core' AND p.proname='analyze_ehr_observation_trend' AND pg_get_userbyid(p.proowner)='insightflow' AND p.proconfig @> ARRAY['search_path=pg_catalog'])" "V27 owner or search_path check failed"
    Assert-True "SELECT has_function_privilege('insightflow_reader','$function','EXECUTE')" "V27 reader lacks EXECUTE"
    Assert-True "SELECT NOT has_function_privilege('public','$function','EXECUTE')" "V27 PUBLIC must not have EXECUTE"
    Assert-True "SELECT NOT has_table_privilege('insightflow_reader','analytics_clinical_staging.stg_public_ehr_observations','SELECT')" "V27 reader must not SELECT patient-keyed staging"
    Assert-True "SELECT to_regclass('analytics_clinical_core.ehr_reference_ranges') IS NOT NULL" "V27 reference catalog table is missing"
    Assert-True "SELECT NOT has_table_privilege('insightflow_reader','analytics_clinical_core.ehr_reference_ranges','INSERT')" "V27 reader must not INSERT reference catalog rows"
    Assert-True "SELECT NOT has_table_privilege('insightflow_reader','analytics_clinical_core.ehr_reference_ranges','UPDATE')" "V27 reader must not UPDATE reference catalog rows"
    Assert-True "SELECT NOT has_table_privilege('insightflow_reader','analytics_clinical_core.ehr_reference_ranges','SELECT')" "V27 reference catalog must be read through the fixed function"
    Assert-True "SELECT has_function_privilege('insightflow_reader','$snapshotFunction','EXECUTE')" "V27 reader lacks snapshot EXECUTE"
    Assert-True "SELECT NOT has_function_privilege('public','$snapshotFunction','EXECUTE')" "V27 snapshot PUBLIC must not have EXECUTE"

    Write-Host "V27 017/018 applied idempotently; aggregate/snapshot functions, reference catalog and ACL checks passed; no automatic rollback." -ForegroundColor Green
} finally {
    Pop-Location
}

