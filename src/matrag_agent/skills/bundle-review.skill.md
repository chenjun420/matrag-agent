---
name: bundle-review
description: 对 MATRag 导出的 bundle 做评审与 plan 解读
phase: B.1（骨架）
agent: review_agent（Phase B.1 规划）
---

# bundle-review（占位）

> Phase B.1 占位文件，仅确立语法与结构；完整工作流在 `review_agent` 落地时补全。

## 触发

- `POST /agent/review`，body 含 `product_code` 或 `bundle`。

## 步骤（规划）

1. `read_bundle(product_code)` —— 读 MATRag `data/exports/{code}.bundle.json`。
2. `plan(bundle)` —— 调 ATEStudio `/aterag/plan`。
3. 三类分离解读：`plan_pending`（待签字）/ `plan_gaps`（缺 binding）/ `warnings`。
4. 输出评审结论与修复建议。

## 红线

- 不代签；不写 DB。
- 数值必须能追溯到 bundle 原文（禁止编造）。
- 工具调用只走白名单（`clients/gateway.py:ALLOWED_TOOLS`）。
