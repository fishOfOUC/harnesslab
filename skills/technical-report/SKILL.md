---
name: technical-report
version: 1.0.0
description: 基于授权资料生成带引用的技术比较报告
required_tools:
  - search_knowledge
  - read_source
  - calculator
  - write_report
input_schema: technical_report_request_v1
output_schema: technical_report_result_v1
---

# 技术比较报告方法

## 适用场景

需要比较多份技术方案、标出冲突与证据、形成选型建议时使用。

## 方法

1. 先检索所有候选方案的关键事实（并发、超时、重试、失败处理）。
2. 对每个结论记录来源标签；两份资料冲突时同时引用并说明版本先后。
3. 数值计算交给 calculator，并在报告中列出输入与假设。
4. 用 `templates/report.md` 的结构写报告，产物写入当前 run 工作区。
5. 没有证据支持的判断写入“未知项”，不要补全。

## 约束

- 本技能只提供方法与模板，不改变工具权限；需要审批的操作仍然需要审批。
- 报告中的引用标签必须来自本次检索返回的证据，不能自造。
