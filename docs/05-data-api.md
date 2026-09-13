# 05 数据模型与 API 契约

> 这是待实现契约。接口示例不代表当前已有服务。所有时间使用 UTC ISO 8601，ID 使用随机 UUID，列表使用游标分页。

## 1. 核心实体

| 实体 | 关键字段与关系 |
| --- | --- |
| `Project` | id、name、owner_id、policy_version、created_at |
| `Thread` | id、project_id、title、active_run_id、revision |
| `Run` | id、thread_id、parent_run_id、status、mode、config_snapshot、budget、lease_owner、lease_expiry、fencing_token、error_code |
| `RunEvent` | run_id、seq、event_id、type、timestamp、payload、schema_version；唯一 `(run_id, seq)` |
| `Approval` | id、run_id、tool_call_id、checkpoint_id、kind、arguments_hash、revision、status、expires_at、reviewer_id |
| `ToolOperation` | id、run_id、logical_action_id、tool_version、idempotency_key、status、request_hash、result_ref、external_receipt |
| `Document` | id、project_id、title、active_version、deleted_at |
| `DocumentVersion` | document_id、version、content_hash、parse_status、source_path、index_version |
| `Chunk` | id、document_id、document_version、project_id、text_ref、position、index_version |
| `IndexManifest` | id、project_id、model_fingerprint、dimension、pipeline_version、status |
| `Artifact` | id、run_id、project_id、relative_path、media_type、hash、size、created_at |
| `MemoryItem` | id、project_id、user_id、content、source_ref、status、revision、expires_at、deleted_at |
| `EvalDataset` / `EvalRun` | dataset_version、cases_hash；config_snapshot、metrics、case_results、artifact_refs |
| `DemoTicket` | id、project_id、title、body、tool_operation_id；操作 ID 唯一 |

框架 checkpoint 表由所选 checkpointer 管理；应用通过稳定 thread ID、checkpoint ID 和图版本引用，不直接耦合其内部字段。

## 2. 一致性规则

1. `project_id` 从已认证上下文和资源关系推导；不能只信任请求体或工具参数。
2. 创建 run 的幂等范围是调用者、接口和项目；同一键同一请求返回原 run，不同请求体返回 409。
3. 一个 thread 同时最多一个非终态 run，通过数据库事务约束；冲突返回 409。
4. 运行租约通过 compare-and-swap 领取，fencing token 单调递增；过期持有者不能提交新的操作结果或调用意图。
5. 工具幂等键基于 run、稳定逻辑动作 ID、工具版本与审批参数版本。不要仅按参数哈希去重，用户可能确实要执行两次相同操作。
6. 工具账本状态为 `prepared / executing / succeeded / failed / uncertain`。状态不明进入人工核对；不假定检查点保证外部 exactly-once。
7. 审批提交同时检查状态、revision、参数哈希、有效期和 run 状态；取消先提交则审批返回冲突。批准先提交后取消，则仍要在工具提交前检查取消信号。
8. 产物只通过 ID 下载；API 不接受任意绝对路径。

## 3. API 清单

统一前缀 `/api/v1`。本机模式仍由服务端注入固定开发身份；公开网络部署需要正式认证后才能启用。

| 方法与路径 | 用途 | 成功状态 |
| --- | --- | --- |
| `GET /health/live` | 进程存活 | 200 |
| `GET /health/ready` | 必需存储可用；模型状态在响应中独立列出 | 200 / 503 |
| `GET /model-profiles` | 脱敏模型配置与已探测能力 | 200 |
| `POST /model-profiles/{id}/probe` | 异步能力探测 | 202 |
| `POST /projects` | 创建项目 | 201 |
| `POST /projects/{id}/documents` | multipart 上传，返回索引任务 ID | 202 |
| `GET /projects/{id}/documents` | 文档、版本、索引状态 | 200 |
| `DELETE /projects/{id}/documents/{document_id}` | 逻辑删除并启动清理 | 202 |
| `POST /projects/{id}/retrieval/preview` | 检索调试，返回各阶段候选 | 200 |
| `GET /jobs/{id}` | 索引、探测等后台任务状态 | 200 |
| `POST /projects/{id}/threads` | 创建会话 | 201 |
| `POST /threads/{id}/runs` | 提交用户消息并启动任务 | 202 |
| `GET /runs/{id}` | 权威状态、摘要、待审批项 | 200 |
| `GET /runs/{id}/events` | SSE 事件流或补播 | 200 |
| `POST /runs/{id}/cancel` | 请求取消；重复请求返回当前状态 | 202 |
| `POST /runs/{id}/retry` | 为终态失败任务创建新 run | 202 |
| `POST /runs/{id}/fork` | 从允许的检查点创建新 thread/run | 202 |
| `GET /runs/{id}/approvals` | 获取待审批操作 | 200 |
| `POST /approvals/{id}/decision` | 批准或拒绝特定版本 | 200 |
| `POST /approvals/{id}/revision` | 修改参数，产生新的待审批版本 | 201 |
| `GET /artifacts/{id}/download` | 授权下载 | 200 |
| `GET /sources/{chunk_id}` | 授权原文与位置 | 200 |
| `GET /projects/{id}/memories` | 查看候选及已确认记忆 | 200 |
| `PATCH /memories/{id}` | 确认或修改记忆，带 revision | 200 |
| `DELETE /memories/{id}` | 删除记忆及派生数据 | 202 |
| `POST /projects/{id}/eval-runs` | 启动指定数据集与配置评测 | 202 |
| `GET /eval-runs/{id}` | 进度、分项结果和聚合指标 | 200 |

导入、评测和模型探测共享后台任务机制，但不能在普通 `Run` 状态里混入索引专属状态。

## 4. 创建运行示例

请求头：`Idempotency-Key: demo-request-001`。

```json
{
  "message": "比较知识库中两个方案，生成有来源的报告",
  "mode": "research",
  "model_profile_id": "local-chat",
  "knowledge_scope": {"document_ids": []},
  "limits": {"max_model_calls": 20, "active_timeout_seconds": 180}
}
```

`document_ids` 为空表示当前项目内所有可访问、已就绪的资料；服务端将实际索引版本写入快照。调用者只能降低普通运行限制，提高上限需要具备策略管理权限。

```json
{
  "run_id": "00000000-0000-4000-8000-000000000001",
  "status": "queued",
  "events_url": "/api/v1/runs/00000000-0000-4000-8000-000000000001/events"
}
```

## 5. 审批示例

```json
{
  "decision": "approve",
  "expected_revision": 2,
  "arguments_hash": "sha256:example",
  "comment": "确认创建本地演示工单"
}
```

`reviewer_id` 从身份上下文获取。客户端不能用本请求直接提交修改后的工具参数；应先调用 revision 接口，再审批新版本。审批成功表示决策已保存，实际执行结果通过 run 事件查询。

## 6. SSE 事件契约

每条持久事件包含 `schema_version`、`event_id`、`run_id`、单调递增 `seq`、`timestamp`、`type`、`payload`。SSE 的 `id` 使用该 run 的 seq，重连请求携带 `Last-Event-ID`。

```text
id: 17
event: tool.completed
data: {"schema_version":1,"event_id":"evt-17","run_id":"run-example","seq":17,"timestamp":"2026-09-13T08:00:00Z","type":"tool.completed","payload":{"tool_call_id":"call-2","name":"search_knowledge","ok":true,"duration_ms":231}}

```

事件集合：`run.queued`、`run.started`、`plan.updated`、`message.delta`、`message.completed`、`retrieval.completed`、`tool.started`、`tool.completed`、`approval.requested`、`approval.decided`、`artifact.created`、`budget.updated`、`run.recovering`、`run.completed`、`run.failed`、`run.cancelled`。

所有带 seq 的事件先持久化再发送；Token 可聚合成短文本块以减少写入。心跳使用 SSE 注释，不占业务 seq。交付语义为至少一次，客户端按 seq 去重。`message.delta` 携带消息 ID 与块序号；`message.completed` 携带完整文本作为重建依据。

保留期内补播全部缺失事件；游标过旧返回 410 和快照入口，客户端以 `GET /runs/{id}` 重建，再从最新水位订阅。鉴权失败不建立流；慢客户端断开后可重连，不阻塞工作进程。使用 fetch 流时带认证头；如果采用浏览器原生 EventSource，则使用受控同源会话方案。

## 7. 错误格式

```json
{
  "error": {
    "code": "EMBEDDING_UNAVAILABLE",
    "message": "本地向量服务不可用，请检查 LM Studio 服务状态",
    "retryable": true,
    "request_id": "req-example",
    "details": {}
  }
}
```

主要错误：`VALIDATION_ERROR` 422、`UNAUTHORIZED` 401、`FORBIDDEN` 403、`NOT_FOUND` 404、`RUN_CONFLICT` 409、`APPROVAL_CONFLICT` 409、`INDEX_VERSION_MISMATCH` 409、`MODEL_CAPABILITY_MISSING` 422、`RATE_LIMITED` 429、`EMBEDDING_UNAVAILABLE` 503。

已接受的异步任务失败，通过 run/job 状态报告 `BUDGET_EXCEEDED`、`TOOL_TIMEOUT`、`OUTPUT_VALIDATION_FAILED`、`SIDE_EFFECT_UNCERTAIN` 等业务错误；不把原始创建请求追溯修改成 HTTP 500。响应不包含密钥、宿主绝对路径或完整堆栈。
