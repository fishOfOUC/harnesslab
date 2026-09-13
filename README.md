# HarnessLab：LangChain 技术展示项目

> 状态：**M0（环境与契约）+ M1（最小问答闭环）已实现并通过自动化验证；M2（运行控制内核）主体已实现**，M3～M6 见下方能力状态表。
> 设计日期 2026-09-13；实现日期 2026-09-13。所有指标与成绩以“实际执行过的验证”为准，未执行的一律标注“未执行”。

HarnessLab 是一个面向技术展示的知识工作 Agent 工作台：导入技术资料后，让 Agent 检索证据、拆解任务、调用工具、生成报告，并在同一界面查看执行轨迹、审批操作、恢复中断任务和比较检索评测结果。

核心目标是把 Agent Harness 的工程能力展示清楚：模型如何获得上下文、如何使用工具、如何受到权限与预算约束，以及失败后如何继续执行。

## 快速开始

前置条件：Python ≥ 3.11、[uv](https://docs.astral.sh/uv/)、Node.js ≥ 20；LM Studio 与一个 Embedding 模型（可选一个通过工具调用实测的 Chat 模型）。

```powershell
# 1. 后端依赖（会生成并提交 uv.lock）
cd backend
uv sync

# 2. 配置模型：复制模板并按本机 LM Studio 实际值填写
copy ..\.env.example ..\.env
#   至少填写 EMBEDDING_MODEL 与 CHAT_MODEL 的真实模型 ID

# 3. 环境检查（--probe 会真实调用 LM Studio）
uv run harnesslab doctor --probe

# 4. 初始化业务库与检查点库
uv run harnesslab db migrate

# 5. 启动 API + 内联工作进程（单机模式下 Local 向量库只允许一个进程访问）
uv run harnesslab api serve --port 8000
```

前端二选一：

```powershell
# A. 单端口：构建后由后端直接托管，访问 http://127.0.0.1:8000
cd frontend; npm install; npm run build

# B. 开发模式：热更新，访问 http://127.0.0.1:5173（/api 代理到后端）
cd frontend; npm install; npm run dev
```

导入演示资料并跑一次端到端检查：

```powershell
uv run harnesslab demo seed          # 幂等导入 datasets/demo 的合成资料
pwsh ../scripts/smoke_e2e.ps1        # 启动服务 → 导入 → 检索 → 提交运行 → 落终态
```

> `.ps1` 脚本含中文，请用 PowerShell Core（`pwsh`）执行；Windows PowerShell 5.1 会按 ANSI 解码导致语法错误。

无 Chat 模型时的降级：设置 `CHAT_PROVIDER=stub`。界面与报告会把替身结果标注为“替身模式”，不得作为真实模型成绩。

## 统一命令入口

| 命令 | 作用 |
| --- | --- |
| `harnesslab doctor [--probe]` | 检查配置、存储目录权限、模型连通性与维度一致性 |
| `harnesslab db migrate` | 执行业务迁移并初始化 checkpointer 表 |
| `harnesslab api serve [--port] [--worker/--no-worker]` | 启动 API 与 SSE（默认内联工作进程） |
| `harnesslab worker serve` | 独立工作进程（需把 `VECTOR_MODE` 切到 `service`，或让 API 不启动工作进程） |
| `harnesslab demo seed [--project]` | 幂等创建合成演示资料并排队导入 |
| `harnesslab index rebuild [--project]` | 暂存重建索引、验证后原子切换，旧索引保留回滚 |
| `harnesslab eval run [--project] [--mode]` | 运行检索质量评测并输出报告 |
| `harnesslab data export` / `data restore` | 一致性备份与恢复校验 |

## 目录结构

```text
backend/src/harnesslab/
  api/             # 路由、身份与项目作用域、SSE、统一错误格式
  runtime/         # 工作进程、租约、取消、恢复对账、运行执行器
  agents/          # create_agent 工厂、检查点接线、Prompt 与 Skills 加载
  harness/         # 上下文装配与压缩、预算、策略、模型路由与能力探测
  knowledge/       # 解析、切分、Embedding 适配、Qdrant 索引、混合检索、引用
  tools/           # 工具注册表、工具网关（策略+预算+审批+幂等账本）、受限工作区
  storage/         # 业务 SQLite schema、仓储与一致性规则
  observability/   # 脱敏结构化日志与运行事件
  evals/           # 检索评测 Runner 与报告
  cli.py / demo.py
frontend/src/      # 概览、任务工作台、知识库、审批中心、评测中心、设置
configs/ prompts/ skills/ datasets/demo/ scripts/ docs/
```

## 已实现能力与状态

“已验证”指有自动化测试或真实服务运行记录；标注“未执行”的能力没有实测成绩。

| 需求 | 能力 | 状态 | 验证方式 |
| --- | --- | --- | --- |
| H01 | 模型接入与能力探测 | 已实现，真机未探测 | `doctor --probe` 与 `/model-profiles/{id}/probe` 真实调用 LM Studio；本机结果待填入文档 11 |
| H02 | Agent 循环与结构化结果 | 已实现并验证 | 脚本化替身驱动 `create_agent`，最终答复经 `AgentAnswer` Schema 校验，失败保留原文并标注限制 |
| H03 | 会话与短期记忆 | 已实现 | thread 级消息状态 + 文件检查点；同 thread 只允许一个非终态 run（数据库约束 + 测试） |
| H04 | 工具注册与输入输出校验 | 已实现并验证 | 工具经 LangChain 签名校验后才进入网关；非法参数无副作用（用例覆盖） |
| H05 | RAG 与来源引用 | 已实现并验证 | 导入 5 份演示资料 → 混合检索 → `[S1]` 标签映射到固定文档版本与字符/页码定位；删除后立即阻断检索与原文 |
| H06 | 持久化与恢复 | 已实现并验证（替代故障注入） | 租约 CAS + fencing token、过期租约进入 `recovering`、结果不明的副作用进入人工核对且不自动重试 |
| H07 | 流式事件与取消 | 已实现 | 事件先落库后发送、seq 唯一、`Last-Event-ID` 补播、游标过旧返回 410；取消在模型/工具边界检查 |
| H08 | 步数、时长与用量预算 | 已实现并验证 | 模型/工具/时长/token 预算、循环检测；耗尽落 `BUDGET_EXCEEDED` 并保留已有产物 |
| H09 | 可观测性与基础评测 | 已实现 | run 事件表 + 运行时间线 + 配置快照；检索评测输出 Recall@k / MRR / 无答案处理 |
| H10 | 计划、任务列表与重规划 | 部分实现 | 计划生成、步骤状态与事件已实现；重规划**未接线**（仅保留上限配置） |
| H11 | 人工审批与参数修改 | 已实现并验证 | `interrupt()` 中断 + 审批实体落库；批准/拒绝/修订版本、过期、双批准冲突均有用例 |
| H12 | 上下文预算与压缩 | 已实现，质量回归未执行 | 装配顺序与 80% 压缩阈值；压缩保留用户约束、审批状态、产物与引用 ID |
| H13 | 长期记忆与删除 | 已实现并验证 | 候选/确认/修订/删除 + revision 冲突；删除立即阻断读取 |
| H14 | 混合检索与可选重排 | 部分实现 | 向量 + 中文 BM25 + RRF 与检索实验室已实现；**重排模型未接入** |
| H15 | Skills 与 Prompt 版本 | 已实现 | `skills/*/SKILL.md` 加载、内容哈希进入 run 快照；`required_tools` 不提升权限（用例覆盖） |
| H16 | MCP 扩展工具 | 未实现（P1） | — |
| H17 | 受限文件工作区与产物 | 已实现并验证 | 绝对路径/盘符/UNC/`..`/备用数据流/受限设备名/符号链接逃逸全部拒绝；产物按 ID 下载 |
| H18 | 多 Agent 协作 | 未实现（P1） | 子任务协议 `Findings` 已定义 |
| H19 | 权限、注入防护与审计 | 部分实现 | 服务端权威策略、未知工具默认拒绝、审核顺序完整、注入文本按不可信块处理并写入限制项；**对抗测试回归未执行** |
| H20 | 回放、评测比较与版本追踪 | 部分实现 | 时间线读取、重跑（新 run）、检查点分叉、检索评测对照已实现；**回放页与配置对比页未实现** |
| H21–H25 | 沙箱、定时任务、多模态、分布式、Deep Agents | 未实现（P2） | 沙箱关闭时后端直接拒绝 `run_python_sandbox`，界面不提供入口 |

## 关键设计取舍（实现期补充的决策）

1. **审批用工具内 `interrupt()`，不用框架中间件**：审批载荷（参数哈希、修订版本、checkpoint_id）必须落到业务表，框架通用中断无法表达“一次批准只绑定一个参数版本”，同时使用两套会产生互不知晓的审批记录。
2. **`create_agent` 直接作为运行图，确定性业务流由 runtime 层显式实现**：队列、租约、状态机、审批与恢复对账都在应用层，避免内外双层模型/工具循环冲突；代价是计划/收尾不在图检查点内（计划无副作用，重算安全）。
3. **API 进程默认内联工作进程**：Qdrant Local 只允许单进程访问目录，拆成两个进程会让检索接口与索引任务互相抢锁。要独立工作进程时请把 `VECTOR_MODE` 切到 `service`。
4. **估算优于伪精确**：无本地 tokenizer 时 token 数量按保守公式估算并在结果里标注 `estimate`；价格不可用时显示“未知”，不填零。
5. **PostgreSQL / 服务化向量库 / 多工作进程未实现**：`Database` 与 `VectorIndex` 已做接口隔离，迁移时需做导出校验、索引重建与恢复演练，不只是替换连接串。

## 验证记录

```powershell
cd backend; uv run pytest          # 99 项自动化测试
cd frontend; npm run build         # 类型检查 + 构建
pwsh scripts/smoke_e2e.ps1         # 真实服务端到端（替身模型）
```

测试覆盖：路径与策略单元、预算与循环检测、仓储一致性（幂等/租约/审批竞态）、知识库导入与检索、工具网关（幂等账本/超时/取消前置检查）、运行执行器（引用校验/审批中断恢复/预算/人工核对）、工作进程、API 契约与评测接口。

浏览器端走查（2026-09-13，Chrome + 真实后端，替身模型）：概览、任务工作台、审批中心三个页面正常渲染，控制台零错误；导入 5 份资料后 24 个 chunk 可检索；提问运行落 `completed` 并展示用量、时间线与限制说明；审批卡的参数修订成功产生 v2 待审批版本，v1 自动变为 `superseded`。

**未执行的验证**：真实 LM Studio Embedding 维度与中文召回实测、真实 Chat 模型工具调用与多轮对话（当前审批链路由脚本化替身模型驱动验证）、进程 kill 级故障演练、注入与越权对抗回归、性能（排队/首 Token/p95）。

## 已知限制与排障

| 现象 | 优先检查 |
| --- | --- |
| Embedding 404 / 401 | base URL 是否以 `/v1` 结尾；LM Studio 是否启用认证 |
| `INDEX_VERSION_MISMATCH` | 更换了 Embedding 模型或前缀 → 执行 `harnesslab index rebuild`，不要清空数据 |
| 向量库被占用 | 不要同时运行 `api serve`（含内联工作进程）与 `worker serve` |
| 重启后任务丢失 | 检查是否误用内存检查点；thread ID 是否稳定（本项目用 run_id 作为 thread_id） |
| 工单重复 | 工具幂等键 = run + 逻辑动作 ID + 工具版本 + 审批参数版本；查看运行详情的工具账本 |
| 页面消息重复 | 客户端按 seq 去重；补播使用 `Last-Event-ID` |
| Agent 不调用工具 | 用 `harnesslab doctor --probe` 检查 Chat 模型是否支持工具调用；不支持会被明确提示 |

安全边界：本机开发模式仅绑定 loopback，使用服务端固定身份；进入局域网或公网前必须实现正式认证与资源授权，不能把单用户模式当作匿名公开服务。

## 文档导航

| 文档 | 主要内容 |
| --- | --- |
| [01 产品需求与能力矩阵](docs/01-product-requirements.md) | 展示定位、Harness 覆盖范围、优先级和可验收需求 |
| [02 系统架构与技术选型](docs/02-architecture.md) | 模块边界、架构图、存储、目录规划、设计决策 |
| [03 Agent Harness 详细设计](docs/03-agent-harness.md) | 运行循环、状态机、上下文、工具、审批、恢复、多 Agent |
| [04 RAG 与 LM Studio 接入](docs/04-rag-lm-studio.md) | 本地 Embedding 检查、适配、索引版本、检索与引用 |
| [05 数据模型与 API 契约](docs/05-data-api.md) | 数据实体、幂等规则、接口、流式事件和错误码 |
| [06 工作台与演示脚本](docs/06-ui-demo.md) | 页面结构、交互状态、十五分钟演示与降级方案 |
| [07 评测与测试方案](docs/07-evaluation.md) | 数据集、评价指标、恢复测试、安全测试、发布门槛 |
| [08 本地开发与运维](docs/08-development-operations.md) | 依赖策略、配置、启动顺序、资源控制、排障 |
| [09 实施计划与任务清单](docs/09-roadmap.md) | 分阶段交付、依赖顺序、完成标准、风险和待确认项 |
| [10 安全与扩展协议](docs/10-security-extensions.md) | 信任边界、沙箱、Skills、MCP、审计和数据治理 |
| [11 技术来源与决策记录](docs/11-references-decisions.md) | 官方资料、适用范围、关键技术决策 |
| [实施工作报告](reports/2026-09-13-实施工作报告.md) | 本次交付物清单、H01–H25 覆盖矩阵、验证记录与遗留项 |

## 实测版本基线

设计文档写 Python 3.11；本机实际使用 **Python 3.12**，依赖由 `backend/uv.lock` 锁定，实测组合：

`langchain 1.4.0` · `langgraph 1.2.11` · `langchain-openai 1.6.2` · `langgraph-checkpoint-sqlite 3.1.1` · `qdrant-client 1.19.0` · `fastapi`（starlette 1.6.0）· React 19 + Vite 7。

升级依赖需重跑：图恢复、消息序列化、Embedding 适配与审批竞态测试，并保留旧版本工作进程处理未完成任务。
