# LM Studio 独立连通性检查（对应文档 04 第 2 节）。
# 可在项目尚未启动时单独执行：只依赖 LM Studio 服务本身。

param(
    [string]$BaseUrl = 'http://localhost:1234/v1',
    [string]$EmbeddingModel = '',
    [string]$ChatModel = ''
)

$headers = @{}
if ($env:EMBEDDING_API_KEY) {
    $headers['Authorization'] = "Bearer $env:EMBEDDING_API_KEY"
}

Write-Host "== 1. 模型列表 ==" -ForegroundColor Cyan
try {
    $models = Invoke-RestMethod -Uri "$BaseUrl/models" -Headers $headers
    $models.data | Select-Object id | Format-Table
} catch {
    Write-Host "模型列表请求失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "检查项：base URL 是否包含 /v1；服务是否已启动；是否启用了认证。" -ForegroundColor Yellow
    exit 1
}

if (-not $EmbeddingModel) {
    Write-Host "未提供 -EmbeddingModel，跳过向量化检查。请从上面的列表中挑选真实模型 ID。" -ForegroundColor Yellow
} else {
    Write-Host "== 2. Embedding 检查 ==" -ForegroundColor Cyan
    $body = @{
        model           = $EmbeddingModel
        input           = @('本地向量检索测试', 'Agent任务恢复与检查点')
        encoding_format = 'float'
    } | ConvertTo-Json -Depth 4
    try {
        $result = Invoke-RestMethod -Uri "$BaseUrl/embeddings" -Method Post -Headers $headers `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
        $result.data | ForEach-Object {
            [PSCustomObject]@{ Index = $_.index; Dimension = $_.embedding.Count }
        } | Format-Table
        Write-Host "检查：返回条数与输入一致；维度一致；元素为有限数值且非零。" -ForegroundColor Green
    } catch {
        Write-Host "Embedding 调用失败：$($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

if ($ChatModel) {
    Write-Host "== 3. Chat 工具调用能力检查 ==" -ForegroundColor Cyan
    $chatBody = @{
        model    = $ChatModel
        messages = @(@{ role = 'user'; content = '请调用工具计算 1+2' })
        tools    = @(@{
                type     = 'function'
                function = @{
                    name        = 'calculator'
                    description = '计算数学表达式'
                    parameters  = @{
                        type       = 'object'
                        properties = @{ expression = @{ type = 'string' } }
                        required   = @('expression')
                    }
                }
            })
    } | ConvertTo-Json -Depth 8
    try {
        $chat = Invoke-RestMethod -Uri "$BaseUrl/chat/completions" -Method Post -Headers $headers `
            -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($chatBody))
        $calls = $chat.choices[0].message.tool_calls
        if ($calls) {
            Write-Host "工具调用：支持（模型返回了 tool_calls）" -ForegroundColor Green
        } else {
            Write-Host "工具调用：未观察到 tool_calls；Agent 可能无法主动使用工具。" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "Chat 调用失败：$($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "`n完成。请把实测模型 ID、维度与结论填入 docs/11-references-decisions.md 的待填实测记录。" -ForegroundColor Cyan
