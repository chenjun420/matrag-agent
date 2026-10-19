---
name: debug-cycle
description: 执行失败 → failure_indexer 审计 → RAG → LLM 综合 Top-3 根因 + 推荐下一步
phase: B.4 debug_agent
agent: debug_agent（matrag_agent.agents.debug_agent.run_debug_agent）
---

# debug-cycle（debug_agent）

execution 失败 → ATEStudio `failure_indexer.audit_report` 失败链路审计 → MATRag
gateway `/ask` 检索已知 root cause patterns → LLM 综合 **Top-3 根因 + 每条推荐下一步**。

## 触发

- `POST /agent/debug`，body 含 `execution_id`（+ 失败 step_id / 设备读数 / plan_yaml）。
- 入口函数：`run_debug_agent(execution_id, llm, gateway_client, atestudio_client, *, rag_query=None)`。

## 输入

- `execution_id`：执行记录 id（= run_id，必填）。
- `failed_step_id`：失败 step id（可选，参与默认检索串）。
- `device_readings`：设备读数（可选，如 `result_measured_value` / `result_passed`，参与综合）。
- `plan_yaml`：plan 摘要（可选，参与综合）。
- 依赖注入：LLM 门面、MATRag gateway 客户端（只读检索）、ATEStudio 客户端。

## 工作流

1. `query_exec(execution_id)` —— 调 ATEStudio `failure_indexer.audit_report`（Phase A.3
   纯函数：只读 Execution + step_results + test_conditions，**不写库、不订阅 NATS**）。
   返回 `execution_status` / `used_requirement_codes` / `measured_conditions` /
   `failed_steps` / `pending_signoffs` 等。
2. RAG 检索（`rag_search`）—— 经 MATRag gateway `/ask` 检索已知 root cause patterns；
   失败**降级但留痕**（记入 `degradations`，不静默）。默认检索串由失败步骤名/原因 +
   执行状态 + 需求编号拼装。
3. LLM 综合（`complete_json`）—— 结合审计 + 读数 + plan_yaml + RAG 上下文，输出
   Top-3 根因（按可能性降序，每条附证据来源）+ 一一对应的推荐下一步。
4. 输出诊断报告（根因 + 建议）。

## 输出（`DebugAgentResult`）

- `audit_summary`：`failure_indexer.audit_report` 原始报告。
- `rag_context`：gateway `/ask` 检索到的 chunk（`text` / `source` / `score`）。
- `root_causes`：Top-3 根因（`description` / `evidence` / `likelihood`）。
- `recommendations`：推荐下一步（与 `root_causes` 一一对应）。
- `confidence`：`high` / `medium` / `low`（由 top 根因 `likelihood` 决定：
  ≥0.7 → high，≥0.4 → medium，否则 low）。
- `degradations`：RAG / LLM 降级留痕（空表示全链路正常）。

## 推荐下一步（枚举）

每条推荐落在四类动作之一：**重测** / **调整夹具** / **调整激励** / **走工程评审**。

## 工具（白名单）

- `ALLOWED_TOOLS = {query_exec, rag_search}`。
- `DENIED_TOOLS`：`import_bundle` / `approve_condition` / `ingest_document` /
  `build_domain_kb` / `add_knowledge`（写 / 代签 / 落知识库，一律拒绝）。
- 每次工具调用前经 `check_tool(name)` 闸门；白名单外 fail-fast。

## S4 指标（验收）

- 样本 ≥30；「命中」= top-3 含真实根因；阈值 ≥70%。

## 红线

- **不写 DB**；**不落 FMEA 知识库**（`add_knowledge` 走 C.1 人工闸门）。
- **不代签**；结论必须标注证据来源，输入外的事实不得编造。
- LLM / RAG 失败一律 fail-fast 或降级留痕，绝不静默吞错。
