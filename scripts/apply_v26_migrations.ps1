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

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Push-Location $repoRoot
try {
    Step "1/3 等待 PostgreSQL 就绪"
    docker compose up -d --wait $ComposeService
    Assert-ExitCode "PostgreSQL 未能进入 healthy 状态"
    docker compose exec -T $ComposeService psql -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName -c "SELECT 1" | Out-Host
    Assert-ExitCode "PostgreSQL 连接检查失败"

    function Invoke-Migration([string]$MigrationFile) {
        $path = Join-Path $repoRoot "database\init\$MigrationFile"
        if (-not (Test-Path -LiteralPath $path)) { throw "迁移文件不存在：$path" }
        Step "执行 $MigrationFile"
        # ON_ERROR_STOP 让任何一条语句失败即停止；本脚本没有自动回滚或删除既有数据。
        $sql = Get-Content -Raw -LiteralPath $path
        $sql | docker compose exec -T $ComposeService psql -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName
        Assert-ExitCode "$MigrationFile 执行失败；已停止，未尝试破坏性回滚"
    }

    Invoke-Migration "015_investigation_versioning.sql"
    Invoke-Migration "016_temporal_outbox_claims.sql"
    Invoke-Migration "019_graph_canonical_state.sql"

    Step "3/3 验证 V26 必需结构"
    $check = @(
        "SELECT to_regclass('enterprise.investigations') IS NOT NULL",
        "SELECT to_regclass('enterprise.temporal_signal_outbox') IS NOT NULL",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='enterprise' AND table_name='investigations' AND column_name='request_id')",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='enterprise' AND table_name='temporal_signal_outbox' AND column_name='claim_id')",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='enterprise' AND table_name='temporal_signal_outbox' AND column_name='claim_expires_at')",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='enterprise' AND table_name='investigations' AND column_name='graph_state_json')",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='enterprise' AND table_name='investigations' AND column_name='graph_state_version')"
    )
    foreach ($query in $check) {
        $result = docker compose exec -T $ComposeService psql -At -v ON_ERROR_STOP=1 -U $DatabaseUser -d $DatabaseName -c $query
        Assert-ExitCode "V26 迁移结构检查失败：$query"
        if (($result | Out-String).Trim() -ne "t") { throw "V26 迁移结构检查未通过：$query" }
    }

    Write-Host "V26 迁移已幂等执行并通过结构检查；未执行自动回滚。" -ForegroundColor Green
} finally {
    Pop-Location
}

