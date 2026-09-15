param([switch]$SkipDockerBuild)

$ErrorActionPreference = "Stop"

function Step([string]$Title) {
    Write-Host "`n==> $Title" -ForegroundColor Cyan
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Wait-BackendReady {
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            $ready = Invoke-RestMethod http://127.0.0.1:18000/ready -TimeoutSec 2
            if ($ready.status -eq "ready" -and $ready.version -match '^\d+\.\d+\.\d+$') { return $ready }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw "后端未在限定时间内进入 ready 状态"
}

$repoRoot = Resolve-Path "$PSScriptRoot\.."
Push-Location $repoRoot
try {
    Step "1/6 配置与密钥泄漏检查"
    docker compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose 配置无效" }
    # 分段拼接，避免扫描规则本身被 git grep 当作泄漏。
    $secretPattern = "s"+"k-[A-Za-z0-9_-]{20,}|DEEPSEEK_API_KEY=s"+"k-"
    $trackedSecrets = git grep -n -E $secretPattern -- . 2>$null
    Assert-True ($LASTEXITCODE -eq 1) "Git 跟踪文件中疑似存在真实 API Key"
    Write-Host "配置有效；跟踪文件未发现 API Key。"

    Step "2/5 Python 运行时自检"
    $env:PYTHONPATH = ".;backend"
    python -m compileall -q backend/app data_generator
    if ($LASTEXITCODE -ne 0) { throw "Python 运行时自检失败" }
    Write-Host "Python 应用代码编译通过。"

    Step "3/5 前端生产构建"
    Push-Location frontend
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端生产构建失败" }
    } finally { Pop-Location }
    Write-Host "前端生产构建通过。"

    Step "4/5 公开临床数据兼容性"
    if (Test-Path ".data/public_clinical") {
        python scripts/validate_public_clinical_data.py --input-dir .data/public_clinical
        if ($LASTEXITCODE -ne 0) { throw "公开临床数据域验收失败" }
    } else {
        Write-Host "未发现本地 .data/public_clinical，跳过大数据验收（不影响代码测试）。" -ForegroundColor Yellow
    }

    Step "5/5 Docker 当前系统启动"
    docker compose up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL 启动失败" }
    docker compose exec -T postgres psql -U insightflow -d insightflow -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/013_clinical_domain_records.sql
    if ($LASTEXITCODE -ne 0) { throw "通用数据域迁移失败" }
    if (-not $SkipDockerBuild) {
        docker compose build backend frontend
        if ($LASTEXITCODE -ne 0) { throw "Docker 镜像构建失败" }
    }
    docker compose up -d --force-recreate backend clinical-worker frontend
    $ready = Wait-BackendReady
    Write-Host ("服务就绪：status={0} version={1}" -f $ready.status,$ready.version)

    Step "附加：动态调查 API 冒烟测试"
    $headers = @{"X-InsightFlow-User"="admin";"X-Request-Id"=("v9-verify-"+[Guid]::NewGuid())}
    # 使用 ASCII 冒烟问题，避免 Windows PowerShell 5.1 在不同系统代码页下改写请求正文。
    $body = @{trial_id="TRIAL-CF-101";question="Which study site has the most abnormal data quality?";provider="fake"} | ConvertTo-Json
    $result = $null
    # HTTP ready 只证明 API 进程已启动；冷启动后的数据库查询链可能还需要短暂预热。
    # 这里轮询真实的多步调查能力，而不是放宽对 Agent 行为的断言。
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        $headers["X-Request-Id"] = "v9-verify-"+[Guid]::NewGuid()
        $result = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18000/api/v8/clinical/investigations -Headers $headers -ContentType "application/json" -Body $body
        if ($result.steps.Count -ge 2) { break }
        Start-Sleep -Seconds 1
    }
    Assert-True ($result.steps.Count -ge 2) "动态调查没有形成多步执行"
    Assert-True ($result.evidence.Count -ge 1) "动态调查没有形成证据"
    Assert-True ($result.status -in @("pending_approval","inconclusive")) "动态调查返回了非法状态"
    $badEvidence = @($result.evidence | Where-Object {
        $_.observation_signal -in @("no_data","insufficient_data") -and
        ($_.supports.Count -gt 0 -or $_.contradicts.Count -gt 0)
    })
    Assert-True ($badEvidence.Count -eq 0) "无数据或小样本被错误归因为支持/反证"

    Write-Host "`nInsightFlow Clinical 当前版本验收通过" -ForegroundColor Green
    Write-Host ("版本={0} 后端=通过 前端=通过 公开数据域=通过/可跳过 动态调查步骤={1} 证据={2} 结论状态={3}" -f $ready.version,$result.steps.Count,$result.evidence.Count,$result.status)
} finally {
    Pop-Location
}

