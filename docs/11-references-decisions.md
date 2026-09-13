# 11 技术来源与决策记录

## 1. 资料使用说明

以下官方文档于 2026-09-13 查阅，用于确认框架分层、Agent 接口、持久化、中断、中间件、MCP 及 LM Studio Embedding 接入方向。网站内容会更新；正式实现时需要冻结依赖并以实际版本做契约测试。

产品需求、API、数据模型、默认预算、评测阈值、目录规划和阶段估时均为本项目设计，不是官方框架保证，也不是已经完成的本机验证结果。

## 2. 官方参考

| 来源 | 本项目使用范围 |
| --- | --- |
| [LangChain：Runtimes, frameworks, and harnesses](https://docs.langchain.com/oss/python/concepts/products) | 区分 LangChain、LangGraph、Deep Agents 的职责 |
| [LangChain：Agents](https://docs.langchain.com/oss/python/langchain/agents) | `create_agent` 作为基础 Agent 构建入口 |
| [LangChain：Middleware](https://docs.langchain.com/oss/python/langchain/middleware/overview) | Agent 执行生命周期中的行为扩展 |
| [LangGraph：Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | 检查点与跨会话存储的职责区分 |
| [LangGraph：Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | 人工中断与恢复基础机制 |
| [LangChain：MCP](https://docs.langchain.com/oss/python/langchain/mcp) | MCP 适配工具的集成入口 |
| [LM Studio：Embeddings](https://lmstudio.ai/docs/developer/openai-compat/embeddings) | OpenAI 兼容的本地向量化接口 |
| [LM Studio：Tool Use](https://lmstudio.ai/docs/developer/openai-compat/tools) | 本地 Chat 工具调用接口；能力仍需模型实测 |
| [LangChain：OpenAIEmbeddings](https://docs.langchain.com/oss/python/integrations/embeddings/openai) | 可选的兼容客户端，需验证本地模型输入行为 |

本文不固定第三方最新小版本号，也不声称当前依赖已经兼容；版本记录由 M0 补齐。

## 3. 设计决策

### ADR-001：采用 LangChain + 显式 LangGraph 编排

状态：设计基线。

目的：让展示能解释模型工具循环与确定性业务控制的区别。基础 Agent 复用框架，持久任务、审批业务记录和产物管理由应用补齐。

代价：需要维护工具账本、图版本及恢复协调器。Deep Agents 作为 P2 对比对象，不同时维护两套首版核心运行逻辑。

### ADR-002：Embedding 与 Chat 独立配置

状态：设计基线。

目的：充分使用已具备的本地 Embedding 条件，同时不假定已有 Chat 模型。索引绑定 Embedding 配置指纹，生成绑定 Chat 能力档案。

代价：需要两个模型探测流程，以及共享显存场景下的资源调度。

### ADR-003：先做单机持久化，再服务化

状态：设计基线。

采用业务 SQLite、独立检查点库、单工作进程、Local 向量存储，降低首次展示基础设施成本。

约束：不承诺多工作进程共享本地向量目录；API 不直接访问该目录。服务化时评估 PostgreSQL 和向量数据库服务模式，并执行迁移演练。

### ADR-004：优先使用模拟副作用

状态：设计基线。

报告写入受限工作区，工单写入项目模拟表，能完整展示审批、幂等和失败恢复，同时保持演示可重复。接入真实外部系统时补充服务端凭证、权限和结果查询机制。

### ADR-005：不把恢复等同于 exactly-once

状态：设计基线。

框架恢复负责续跑，工具操作账本和外部幂等机制负责避免重复副作用。无法核对的执行结果必须进入人工处理；该限制在运行详情中可见。

### ADR-006：本地观测为默认，云追踪可选

状态：设计基线。

核心演示不依赖云端追踪服务。保存结构化事件和版本快照即可做回放及基础评测；启用外部追踪前明确脱敏与字段范围。

### ADR-007：全面覆盖，分阶段验收

状态：设计基线。

以 H01–H25 的能力矩阵保持范围清晰；P0 完成闭环，P1 完成主要展示，P2 承载成本较高的扩展。每个能力都有证据要求，避免用路线图代替实现结果。

## 4. 待填实测记录

| 项目 | 当前状态 |
| --- | --- |
| LM Studio 版本、地址、认证 | 未探测 |
| Embedding 模型、revision、维度、前缀 | 未探测 |
| Chat 模型、工具调用、结构化输出能力 | 未探测 |
| 机器内存、显存、可用并发 | 未探测 |
| Python/Node/依赖锁定组合 | 尚未建立 |
| RAG 与 Agent 质量成绩 | 尚未执行 |
| 故障恢复与安全回归 | 尚未执行 |

更新这些记录时附日期、执行环境和结果位置；设计目标与实测结果分开记录。
