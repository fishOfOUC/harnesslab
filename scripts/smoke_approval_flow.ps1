# 真实模型端到端：受控行动 + 人工审批 + 幂等（对应文档 06 场景 B、01 用户故事 4/5）
#
# 流程：提交"生成报告并创建工单" → 运行停在等待审批 → 批准 → 继续执行 → 只产生一份工单
# 需要 .env 中已配置可用的 Chat 模型（本地或云端），且资料已导入。
#
# 用法：pwsh scripts/smoke_approval_flow.ps1 -Port 8300 [-StartServer]

param(
    [int]$Port = 8300,
    [switch]$StartServer = $true,
    [int]$WaitApprovalSeconds = 180,
    [int]$WaitFinishSeconds = 180
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$base = "http://127.0.0.1:$Port/api/v1"
$process = $null

function Wait-RunStatus {
    param([string]$RunId, [string[]]$Targets, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 800
        $detail = Invoke-RestMethod "$base/runs/$RunId"
        if ($detail.run.status -in $Targets) { return $detail }
    }
    throw "等待运行状态超时：期望 $($Targets -join '/')，实际 $($detail.run.status)"
}

if ($StartServer) {
    Write-Host "启动服务（使用 .env 中的真实模型）：$base" -ForegroundColor Cyan
    $process = Start-Process -FilePath $python -ArgumentList '-m', 'harnesslab.cli', 'api', 'serve', '--port', "$Port" `
        -WorkingDirectory $backend -PassThru -WindowStyle Hidden
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-RestMethod "$base/health/live" -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep -Milliseconds 500 }
    }
}

try {
    $project = (Invoke-RestMethod "$base/projects").projects[0]
    if (-not $project) { throw '没有可用项目，请先导入演示资料' }
    $docs = Invoke-RestMethod "$base/projects/$($project.id)/documents"
    Write-Host "项目 $($project.name)：文档 $($docs.documents.Count) 份，chunk $($docs.index.chunk_count) 个" -ForegroundColor Green

    $thread = Invoke-RestMethod -Uri "$base/projects/$($project.id)/threads" -Method Post `
        -ContentType 'application/json' -Body (@{ title = '真实模型审批演示' } | ConvertTo-Json)
    $message = '请比较 gateway v1 和 v2 的方案差异并引用来源，然后生成一份 Markdown 选型报告，最后申请创建一个本地演示工单。'
    $run = Invoke-RestMethod -Uri "$base/threads/$($thread.id)/runs" -Method Post -ContentType 'application/json' `
        -Headers @{ 'Idempotency-Key' = "approval-demo-$([guid]::NewGuid().ToString('N').Substring(0,8))" } `
        -Body (@{ message = $message; mode = 'research' } | ConvertTo-Json)
    Write-Host "已提交运行：$($run.run_id)" -ForegroundColor Green

    $detail = Wait-RunStatus -RunId $run.run_id -Targets @('waiting_approval', 'completed', 'failed', 'cancelled') `
        -Seconds $WaitApprovalSeconds
    Write-Host "1/5 运行状态：$($detail.run.status)" -ForegroundColor Green

    $tools = (Invoke-RestMethod "$base/runs/$($run.run_id)/timeline").operations | Select-Object tool_name, status
    Write-Host "2/5 已发生的工具调用："
    $tools | Format-Table

    if ($detail.run.status -ne 'waiting_approval') {
        Write-Host "运行没有停在审批前（状态 $($detail.run.status)）。若模型未调用工单工具，请查看运行详情中的限制说明。" -ForegroundColor Yellow
        if ($detail.run.result) {
            Write-Host "答案：$($detail.run.result.answer.Substring(0, [Math]::Min(400, $detail.run.result.answer.Length)))"
        }
        exit 0
    }

    $approval = $detail.pending_approval
    Write-Host "3/5 审批请求：工具 $($approval.tool_name)（版本 v$($approval.revision)，有效期至 $($approval.expires_at)）"
    Write-Host "    规范化参数：$($approval.arguments | ConvertTo-Json -Compress)"
    Write-Host "    预期影响：$($approval.expected_effect)"

    $before = (Invoke-RestMethod "$base/projects/$($project.id)/demo/tickets").tickets.Count
    $decision = Invoke-RestMethod -Uri "$base/approvals/$($approval.id)/decision" -Method Post `
        -ContentType 'application/json' `
        -Body (@{
            decision         = 'approve'
            expected_revision = $approval.revision
            arguments_hash   = $approval.arguments_hash
            comment          = '脚本验证：确认创建本地演示工单'
        } | ConvertTo-Json)
    Write-Host "4/5 审批已提交：$($decision.status)，运行已重新入队（$($decision.run_status)）" -ForegroundColor Green

    # 重复批准应返回冲突，证明一次批准只绑定一个参数版本
    try {
        Invoke-RestMethod -Uri "$base/approvals/$($approval.id)/decision" -Method Post -ContentType 'application/json' `
            -Body (@{ decision = 'approve'; expected_revision = $approval.revision; arguments_hash = $approval.arguments_hash } | ConvertTo-Json) | Out-Null
        Write-Host '    ⚠ 重复批准未被拒绝，请检查审批竞态' -ForegroundColor Red
    } catch {
        $code = ($_.ErrorDetails.Message | ConvertFrom-Json).error.code
        Write-Host "    重复批准被拒绝（$code）——符合预期" -ForegroundColor Green
    }

    $final = Wait-RunStatus -RunId $run.run_id -Targets @('completed', 'failed', 'cancelled') -Seconds $WaitFinishSeconds
    Write-Host "5/5 运行终态：$($final.run.status)" -ForegroundColor Green
    if ($final.run.result) {
        $result = $final.run.result
        Write-Host "    答案片段：$($result.answer.Substring(0, [Math]::Min(300, $result.answer.Length)))"
        Write-Host "    引用 $($result.citations_resolved.Count) 条 ｜ 产物 $($result.artifacts.Count) 个 ｜ 限制说明 $($result.limitations.Count) 条"
        Write-Host "    用量：模型 $($result.usage.model_calls) 次 / 工具 $($result.usage.tool_calls) 次，耗时 $($result.elapsed_ms) ms"
        foreach ($artifact in $result.artifacts) {
            Write-Host "    产物下载：$($artifact.download_url)"
        }
        if ($result.limitations.Count -gt 0) {
            Write-Host '    限制说明：'
            $result.limitations | ForEach-Object { Write-Host "      - $_" }
        }
    }

    $after = (Invoke-RestMethod "$base/projects/$($project.id)/demo/tickets").tickets.Count
    $delta = $after - $before
    Write-Host "    工单数量：批准前 $before → 批准后 $after（新增 $delta）" -ForegroundColor Cyan
    if ($delta -eq 1) {
        Write-Host '幂等校验通过：一次批准只产生一份工单。' -ForegroundColor Green
    } else {
        Write-Host "幂等校验异常：新增 $delta 份工单" -ForegroundColor Red
    }
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        Write-Host '服务已停止。' -ForegroundColor Cyan
    }
}
