# 04 RAG 与 LM Studio 接入

## 1. 接入边界

已知本机有 LM Studio 可运行的 Embedding 模型；尚未确认模型标识、向量维度、上下文长度、前缀要求、服务端口、认证设置和实际运行状态。

Embedding 模型用于把文本转换成向量；完整 Agent 仍需一个能够生成文本并通过工具调用测试的 Chat 模型。两者可以由同一 LM Studio 服务提供，也可以来自不同服务，配置必须分离。

LM Studio 提供兼容接口，可通过以 `/v1` 结尾的 base URL 调用 Embedding。具体模型标识使用本机模型列表中的真实值。参考 [LM Studio Embeddings](https://lmstudio.ai/docs/developer/openai-compat/embeddings)。

## 2. 本机连通性检查

在 LM Studio 中加载 Embedding 模型并启动服务。以下 PowerShell 示例可独立执行，不依赖尚未实现的项目；默认地址仅为示例，请与界面实际配置核对。

```powershell
$lmBaseUrl = 'http://localhost:1234/v1'
$lmHeaders = @{}
if ($env:EMBEDDING_API_KEY) {
    $lmHeaders['Authorization'] = "Bearer $env:EMBEDDING_API_KEY"
}
$lmModelList = Invoke-RestMethod -Uri "$lmBaseUrl/models" -Headers $lmHeaders
$lmModelList.data | Select-Object id
```

从结果和 LM Studio 模型信息中确认哪个是 Embedding 模型；模型列表返回成功不等于该模型已加载或可完成向量化。

```powershell
$lmEmbeddingModel = '替换为真实的Embedding模型ID'
$lmRequest = @{
    model = $lmEmbeddingModel
    input = @('本地向量检索测试', 'Agent任务恢复与检查点')
    encoding_format = 'float'
} | ConvertTo-Json -Depth 4
$lmResult = Invoke-RestMethod `
    -Uri "$lmBaseUrl/embeddings" `
    -Method Post `
    -Headers $lmHeaders `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($lmRequest))
$lmResult.data | ForEach-Object {
    [PSCustomObject]@{ Index = $_.index; Dimension = $_.embedding.Count }
}
```

检查：返回两条向量；按 `index` 对应输入；维度一致；元素是有限数值；向量非零。再输入重复文本检查稳定性，输入过长文本确认服务端行为，不接受静默截断而无记录。

若启用 LM Studio 认证，应使用真实 token；文档示例中的占位密钥不是实际认证凭证。无认证情况下，某些客户端要求非空 key，可在客户端适配层使用明确的占位字符串，不能误传到启用认证的服务。

## 3. 项目配置草案

以下是拟实现的项目环境变量，并非 LM Studio 自带变量。`EMBEDDING_DIMENSION=auto` 由启动探测解析后固定到索引清单。

```dotenv
EMBEDDING_PROVIDER=lm_studio
EMBEDDING_BASE_URL=http://localhost:1234/v1
EMBEDDING_MODEL=replace-with-real-embedding-model-id
EMBEDDING_API_KEY=
EMBEDDING_DIMENSION=auto
EMBEDDING_BATCH_SIZE=8
EMBEDDING_TIMEOUT_SECONDS=60
EMBEDDING_MAX_RETRIES=2
EMBEDDING_QUERY_PREFIX=
EMBEDDING_DOCUMENT_PREFIX=
EMBEDDING_NORMALIZE=true

CHAT_PROVIDER=openai_compatible
CHAT_BASE_URL=http://localhost:1234/v1
CHAT_MODEL=replace-with-real-chat-model-id
CHAT_API_KEY=
ALLOW_CLOUD_FALLBACK=false
```

前缀是否为空、是否归一化，以具体 Embedding 模型说明和评测为准；不能对所有模型套用同一 query/document 前缀。中文能力需要用中文查询实际测量。

## 4. LangChain 适配策略

优先编写一个薄的 LangChain `Embeddings` 适配器：实现 `embed_documents`、`embed_query`，以及需要的异步入口；内部 HTTP 请求发送原始字符串和 `encoding_format=float`，按返回 index 排序并验证长度、维度与数值。

理由：本地模型的 tokenizer、输入长度和前缀可能与云端模型不同，需要明确掌控预处理。也可使用 `langchain-openai` 的 `OpenAIEmbeddings`，但必须在冻结版本上验证 `base_url`、原始文本输入、`check_embedding_ctx_length=False` 和编码行为；不能未经测试假定所有 OpenAI 兼容服务完全一致。集成入口参考 [OpenAIEmbeddings](https://docs.langchain.com/oss/python/integrations/embeddings/openai)。

适配器测试包括空输入、单条、批量、Unicode、超时、认证失败、模型卸载、返回乱序、维度变化、非有限数值和批次部分失败。记录 provider 和 model，不记录密钥。

## 5. 文档导入流水线

`上传 → 文件检查 → 解析 → 规范化 → 切分 → 向量化 → 写索引 → 可见性提交`。

1. 文件类型通过扩展名与内容识别双重检查；默认每份 20 MB、PDF 200 页，超限明确拒绝。
2. 原始文件不可变保存，计算内容哈希；同项目相同内容重复导入复用已完成版本。
3. Markdown 按标题与段落切分；PDF 保留物理页码；TXT 保留字符偏移。扫描 PDF 转入不支持状态，不能当空文档索引成功。
4. 初始 chunk 目标约 600 个模型 token，重叠约 80；实际由 tokenizer 和上下文上限决定。无法获得准确 tokenizer 时采用保守字符切分并通过请求检查校准，清楚标注估算。
5. 表格、代码块尽量保持边界；过大块单独切分并保留父块关系。解析失败不阻塞其他文档。
6. 每批向量写入暂存版本，所有预期 chunk 完成后再将文档版本标记 `ready`。查询只看到完整版本。
7. 失败批次按 chunk ID 幂等续传；索引任务拥有独立预算，避免导入抢占对话资源。

Chunk 元数据至少包含：`project_id`、`document_id`、`document_version`、`chunk_id`、`content_hash`、`source_title`、`heading_path`、`page` 或 `char_range`、`index_version`、访问作用域。

## 6. 索引版本与迁移

每份索引清单记录 provider、模型 ID、可获取的模型修订信息或人工 revision、向量维度、前缀、归一化方式、距离度量、解析器与切分配置版本。

缓存键为以上配置指纹加文本哈希，并按项目隔离。即使维度相同，更换模型也不能混用旧向量。模型 ID 相同但模型文件替换时，需要更新 revision 并重建。

新建 collection → 分批重建 → 完整性与抽样召回验证 → 原子切换活动索引指针 → 保留旧索引用于回滚。活跃 run 固定其索引版本；旧版本没有活跃引用且超过保留期后才删除。

启动时若实测维度或配置指纹与活动索引不符，禁用该索引查询并报告 `INDEX_VERSION_MISMATCH`；不自动清空用户数据。

## 7. 检索策略

P0 使用向量召回，`top_k=8` 作为初始值，返回来源与距离。P1 同时运行 BM25 与向量检索，各召回 20 条，用 Reciprocal Rank Fusion 合并，再去重选取 8 条。RRF 的参数及中文分词词典进入配置版本。

可选重排模型单独配置和测量资源；没有重排模型时使用融合排序并明确展示，不把 Embedding 模型当作 cross-encoder 重排器。

所有候选在召回时应用项目与访问范围过滤，融合、原文读取和下载阶段再次校验。索引不支持可靠过滤时改用项目独立 collection，不用“先检索全部再交给模型过滤”。

对明确的多跳问题可生成至多 3 个子查询，每次追加检索计入预算。查询改写保留关键实体、版本号与否定约束。低证据结果触发补充检索或证据不足回答，阈值用评测集校准，不能跨模型复用任意相似度阈值。

## 8. 答案和引用

答案中的 `[S1]` 等标签由后端映射到本次授权召回的 source ID；后端验证 ID 存在、属于当前项目和固定文档版本。前端点击后展示原文和定位。

PDF 使用真实物理页码，Markdown 使用标题路径及字符范围。来源冲突时同时引用并说明版本差异。引用存在性可以程序检查，“证据是否支持结论”需要评测或人工核查，不能混为同一指标。

删除文档时立即阻断后续检索与原文访问，再异步清理向量、BM25、缓存和原文件。历史产物若含该资料摘要需标记或按删除策略清理；界面不能通过历史引用绕过删除状态。

## 9. 接入验收

- 保存模型 ID、revision、实际维度、前缀、批大小、耗时、服务版本和硬件记录。
- 50 条中文短文本批量导入，重复导入无重复 chunk。
- 10 条人工构造查询验证预期来源，另有至少 3 条无答案查询。
- 更换模型指纹时旧索引被拒绝，新索引可切换且可回滚。
- 关闭 LM Studio 时给出明确服务不可用提示；恢复服务后失败任务可继续。
- 本次文档编写未连接或调用你本机的 LM Studio，上述检查留待实现接入阶段执行。
