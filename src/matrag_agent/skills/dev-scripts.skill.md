---
name: dev-scripts
description: 读 bundle + 方法规格 + 仪器绑定 → LLM 合成 ATEStudio DSL v3.2 脚本
phase: B.5 dev_scripts_agent
agent: dev_scripts_agent（matrag_agent.agents.dev_scripts_agent.run_dev_scripts_agent）
---

# dev-scripts（dev_scripts_agent）

Phase B.5 起，本 agent **统一接管** ATEStudio ``script_generator.py`` 的脚本生成职责
（D9 backlog 替代项）。产出 **ATEStudio DSL v3.2 脚本（Python）**，与
``ate_platform.executor`` 兼容，可直接导入 ATEStudio 执行。**不**输出 pytest 脚本。

> ATEStudio ``script_generator.py`` 后续可废弃；本次改动不触碰 ATEStudio 侧代码。

## 触发

- 入口函数：
  `run_dev_scripts_agent(product_code, llm, *, requirement_code=None, extra_requirements=None, ...)`。
- 输入：`product_code`（必填）+ `requirement_code`（可选）+ 自然语言补充要求（可选）。

## 输入数据源（全部从 MATRag 侧只读）

| 数据 | 位置 |
|---|---|
| bundle（条件 / 方法引用） | `data/exports/{product_code}.bundle.json` |
| 方法四字段 | MATRag `data/domain/source/power_test_methods.yaml` |
| 仪器动作绑定 | ATEStudio `config/test_method_bindings.yaml`（无副本时回退硬编码基础集） |

## 工作流

1. `read_bundle(product_code)` —— 读 MATRag 导出的 bundle（本地文件；缺失 fail-fast）。
2. 选目标需求：有 `requirement_code` 则按 `requirement_code` / `spec_requirement_id` 匹配，
   否则覆盖 bundle 内全部需求。
3. 逐条件展开 `(kind, side)`：
   - 有 `method_ref` → `get_method_spec(method_ref)` 取四字段
     （`parameters` / `instruments` / `interfaces` / `metrics`）；
   - `get_binding_spec(kind, side)` 取绑定（`action` / `settle_s` / `destructive` /
     `resource` / `setup_group` / `requires` / `unit`）。
4. 汇总 `dependencies`（绑定 `resource` 名去重排序）、`risks`（destructive / 无绑定）。
5. LLM 合成（`complete`）—— 基于结构化上下文生成 DSL v3.2 Python 脚本。
6. 归一：剥 Markdown 围栏 → 补 DSL 版本头 → `ast.parse` 校验；不合法则回退确定性骨架。

## 输出（`DevScriptsAgentResult`）

- `script_content`：DSL v3.2 脚本（Python，恒可 `ast.parse`；头部含 `# ATE-DSL-VERSION: 3.2`）。
- `script_format`：`ate_dsl_v3.2`。
- `dependencies`：依赖仪器资源名列表（源自绑定表 `resource`）。
- `risks`：风险标注（destructive 步骤需 `power_cycle` 复测；无绑定条件需补绑定表）。
- `degradations`：降级留痕（工具失败 / 方法未命中 / LLM 失败 / 语法回退；空 = 全链路正常）。

## 失败模式

| 场景 | 行为 |
|---|---|
| bundle 缺失 / 非法 | fail-fast（`FileNotFoundError` / `ValueError`），不产出假脚本 |
| `method_ref` 未在方法库中找到 | 回退空规格 + `degradations` 留痕，继续合成 |
| `(kind, side)` 无绑定 | 回退 `action="unsupported"` + `risks` 标注，继续合成 |
| 方法库 / 绑定表文件不可用 | 方法库 fail-fast；绑定表回退硬编码基础集（`source="fallback"` 留痕） |
| LLM 调用失败 / 语法非法 | 回退确定性骨架 + `degradations` 留痕，绝不静默 |

## 工具（白名单）

- `ALLOWED_TOOLS = {read_bundle, get_method_spec, get_binding_spec}`。
- `DENIED_TOOLS`：`import_bundle` / `import_script` / `approve_condition` /
  `ingest_document` / `build_domain_kb` / `add_knowledge`（写 / 代签，一律拒绝）。
- 每次工具调用前经 `check_tool(name)` 闸门；白名单外 fail-fast。

## 红线

- **不调用 ATEStudio HTTP API**：脚本生成完全在 MATRag 端，ATEStudio 后续**导入**即可。
- **不输出 pytest 脚本**：只产出 DSL v3.2 Python 脚本。
- **不写库、不代签**；数值必须能追溯到 bundle 原文。
- destructive 步骤必须显式标注并给出恢复（`power_cycle`）建议。
- 工具 / LLM 失败一律降级留痕，绝不静默吞错。
