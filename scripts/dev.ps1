# 本地开发：同时启动后端（API + 内联工作进程）与前端 dev server。
# 单机模式下 Local 向量库只允许一个进程访问，因此不要同时再启动 `worker serve`。

param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'
$frontend = Join-Path $repo 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host '未找到后端虚拟环境：请先在 backend 目录执行 uv sync' -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
    Write-Host '未安装前端依赖：请在 frontend 目录执行 npm install' -ForegroundColor Red
    exit 1
}

Write-Host "启动后端：http://127.0.0.1:$ApiPort" -ForegroundColor Cyan
$api = Start-Process -FilePath $python `
    -ArgumentList '-m', 'harnesslab.cli', 'api', 'serve', '--port', "$ApiPort" `
    -WorkingDirectory $backend -PassThru

Write-Host "启动前端：http://127.0.0.1:$WebPort（/api 代理到后端）" -ForegroundColor Cyan
$web = Start-Process -FilePath 'npm.cmd' `
    -ArgumentList 'run', 'dev', '--', '--port', "$WebPort" `
    -WorkingDirectory $frontend -PassThru

Write-Host "按 Ctrl+C 停止两个进程。" -ForegroundColor Yellow
try {
    Wait-Process -Id $api.Id, $web.Id
} finally {
    foreach ($process in @($api, $web)) {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
    }
    Write-Host '已停止。' -ForegroundColor Cyan
}
