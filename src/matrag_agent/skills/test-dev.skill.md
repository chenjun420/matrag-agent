---
name: test-dev
description: 自然语言需求 → bundle draft → plan 三类分离解读报告
phase: B.3 dev_agent
agent: dev_agent（matrag_agent.agents.dev_agent.run_dev_agent）
---

# test-dev（dev_agent）

自然语言需求 → RAG 上下文 → LLM 提案 bundle draft → 调 ATEStudio `/aterag/plan`
→ **pending / gap / warnings 三类分离解读**（D16 语义修正，R14 修复）。

## 触发

- `POST /agent/dev`，body 含 `text`（自然语言需求）+ `product_code`。
- 入口函数：`run_dev_agent(product_code, user_query, llm, gateway_client, atestudio_client)`。

## 输入

- 用户自然语言需求（`user_query` / `text`）。
- 产品编码 `product_code`（决定读取哪个 bundle）。
- 依赖注入：LLM 门面、MATRag gateway 客户端（只读检索）、ATEStudio 客户端。

## 工作流

1. `read_bundle(product_code)` —— 读 MATRag 导出的 `data/exports/{code}.bundle.json`
   （本地文件；缺失 fail-fast）。
2. RAG 检索 —— 经 MATRag gateway 只读工具补上下文；失败降级但**留痕**（不静默）。
3. `propose_test(llm, bundle_text)` —— LLM 提案 bundle **draft**：
   - 一律 `status="draft"`（绝不代签）；
   - **数值必见原文**（fact guard：数值逐字见于原文，否则弃该值并落 `review`）；
   - kind/side 必须 ∈ 词表（词表外弃用）；measure 类无判据则整条弃用；
   - LLM 输出非法 → 带更强 `schema_hint` 重试一次，仍失败 → 带 `fallback` 标记的最小 draft。
4. `plan(draft_bundle)` —— 调 ATEStudio `POST /api/v1/imports/aterag/plan`（只读推导）。
5. **三类分离解读**（D16）：
   - `pending_signoffs`：未签字 draft 条件 → 走 `POST /api/v1/knowledge/conditions/approve`
     （`{requirement_id, by, condition_ids?}`，由**人工**签字）。
   - `gaps`：无判据 / 缺 binding 的条件 → 建议补 `test_method_bindings.yaml`。
   - `warnings`：planner 标注的资源冲突 / 占位限制。
6. LLM 综合建议（`llm_interpretation`）：分别说明三类含义与下一步；不得编造输入外事实。

## 输出

- `bundle_draft`：LLM 提案并归一后的 draft bundle（`status="draft"`，含 `review` / `stats`）。
- `pending_signoffs` / `gaps` / `warnings`：三类**分别**列表（`list[dict]`）。
- `llm_interpretation`：LLM 综合建议文本。

## 工具（白名单）

- `ALLOWED_TOOLS = {read_bundle, plan, propose_test}`。
- `DENIED_TOOLS`：`import_bundle` / `approve_condition` / `ingest_document` /
  `build_domain_kb` / `add_knowledge`（写 / 代签，一律拒绝）。
- 每次工具调用前经 `check_tool(name)` 闸门；白名单外 fail-fast。

## 红线

- **不代签**：draft 未经人审不得作为产测判据；签字由人工经 ATEStudio 端点完成。
- **不写 DB**：dev_agent 不直接落库；草案提交走 B.4 或人工。
- **数值必见原文**：不得换算、推断或补全。
- 工具调用只走白名单；跨仓调用一律经 `clients/` 门面。
