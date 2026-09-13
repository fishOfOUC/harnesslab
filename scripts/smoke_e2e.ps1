# HarnessLab 端到端烟雾测试（真实启动 API + 内联工作进程）
# 默认使用替身 Embedding 与替身 Chat，仅验证接口、导入流水线、检索与运行状态机；
# 需要验证真实模型时，去掉 -Fake 并先在 .env 中配置 LM Studio 模型。

param(
    [int]$Port = 8123,
    [switch]$Fake = $true,
    [switch]$KeepData
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$dataDir = Join-Path $repo 'data\smoke'

if (-not (Test-Path $python)) {
    Write-Host "未找到虚拟环境，请先在 backend 目录执行 uv sync" -ForegroundColor Red
    exit 1
}

if ((-not $KeepData) -and (Test-Path $dataDir)) {
    Remove-Item -Recurse -Force $dataDir
}

$env:APP_ENV = 'development'
$env:DATA_DIR = $dataDir
$env:ARTIFACT_DIR = (Join-Path $repo 'artifacts\smoke')
$env:RUN_WORKER_IN_API = 'true'
$env:API_PORT = "$Port"
$env:LOG_LEVEL = 'WARNING'
if ($Fake) {
    $env:EMBEDDING_PROVIDER = 'fake'
    $env:CHAT_PROVIDER = 'stub'
    Write-Host '[提示] 使用替身模型：结果只验证控制流与契约，不代表真实模型能力。' -ForegroundColor Yellow
}

$base = "http://127.0.0.1:$Port/api/v1"
Write-Host "启动服务：$base" -ForegroundColor Cyan
$process = Start-Process -FilePath $python -ArgumentList '-m', 'harnesslab.cli', 'api', 'serve', '--port', "$Port" `
    -WorkingDirectory $backend -PassThru -WindowStyle Hidden

function Wait-Endpoint {
    param([string]$Url, [int]$Seconds = 40)
    for ($i = 0; $i -lt ($Seconds * 2); $i++) {
        try {
            Invoke-RestMethod -Uri $Url -TimeoutSec 3 | Out-Null
            return $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

try {
    if (-not (Wait-Endpoint "$base/health/live")) {
        Write-Host '服务未在预期时间内启动' -ForegroundColor Red
        exit 1
    }
    Write-Host '1/6 服务存活' -ForegroundColor Green
    (Invoke-RestMethod "$base/health/ready").status | ForEach-Object { Write-Host "    就绪状态：$_" }

    $project = Invoke-RestMethod -Uri "$base/projects" -Method Post -ContentType 'application/json' `
        -Body (@{ name = '烟雾测试项目' } | ConvertTo-Json)
    Write-Host "2/6 创建项目：$($project.id)" -ForegroundColor Green

    $seed = Invoke-RestMethod -Uri "$base/projects/$($project.id)/demo/seed" -Method Post
    Write-Host "    排队导入 $($seed.queued.Count) 份演示资料" -ForegroundColor Green

    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Seconds 1
        $docs = Invoke-RestMethod "$base/projects/$($project.id)/documents"
    } while ($docs.index.chunk_count -lt 1 -and (Get-Date) -lt $deadline)
    Write-Host "3/6 索引完成：$($docs.index.chunk_count) 个 chunk，维度 $($docs.index.dimension)" -ForegroundColor Green

    $preview = Invoke-RestMethod -Uri "$base/projects/$($project.id)/retrieval/preview" -Method Post `
        -ContentType 'application/json' -Body (@{ query = 'v2 的并发上限'; top_k = 5 } | ConvertTo-Json)
    Write-Host "4/6 检索命中 $($preview.hits.Count) 条，模式 $($preview.mode)，索引 $($preview.index_version.Substring(0,8))" -ForegroundColor Green
    $preview.hits | Select-Object -First 3 source_title, score | Format-Table

    $thread = Invoke-RestMethod -Uri "$base/projects/$($project.id)/threads" -Method Post -ContentType 'application/json' `
        -Body (@{ title = '烟雾测试会话' } | ConvertTo-Json)
    $run = Invoke-RestMethod -Uri "$base/threads/$($thread.id)/runs" -Method Post -ContentType 'application/json' `
        -Headers @{ 'Idempotency-Key' = 'smoke-001' } `
        -Body (@{ message = '比较 gateway v1 与 v2 的并发上限，引用来源'; mode = 'research' } | ConvertTo-Json)
    Write-Host "5/6 提交运行：$($run.run_id)（$($run.status)）" -ForegroundColor Green

    do {
        Start-Sleep -Seconds 1
        $detail = Invoke-RestMethod "$base/runs/$($run.run_id)"
    } while ($detail.run.status -in @('queued', 'running', 'recovering') -and (Get-Date) -lt $deadline)
    Write-Host "6/6 运行终态：$($detail.run.status)" -ForegroundColor Green
    Write-Host "    事件数：$($detail.last_seq)，待审批：$(if ($detail.pending_approval) { '有' } else { '无' })"
    if ($detail.run.result) {
        Write-Host "    答案片段：$($detail.run.result.answer.Substring(0, [Math]::Min(160, $detail.run.result.answer.Length)))"
        Write-Host "    引用数：$($detail.run.result.citations_resolved.Count)，限制说明：$($detail.run.result.limitations.Count) 条"
        Write-Host "    用量：模型 $($detail.run.result.usage.model_calls) 次 / 工具 $($detail.run.result.usage.tool_calls) 次（token 为估算值）"
    }
    Write-Host "`n烟雾测试完成。数据目录：$dataDir" -ForegroundColor Cyan
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        Write-Host '服务已停止。' -ForegroundColor Cyan
    }
}
