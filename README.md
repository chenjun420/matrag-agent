# matrag-agent

MATRag 产测 Agent 服务（独立仓库，决策 D3）。对外经 **HTTP + SSE**（D7）提供
agent 能力；数据一律经 MATRag gateway / ATEStudio HTTP 获取，**不重新解析规格书**
（spec §1.5：规格书 single source of truth 在 MATRag 主仓）。

## 当前状态（2026-10-18，架构升级全面落地后）

**128 passed + 1 skipped**，已实现 B.1 骨架 + B.3 `dev_agent`（26 测试）+ B.4 `debug_agent`（32 测试）+ B.5 `dev_scripts_agent`（30 测试；含 HT-A 归档迁移）。对外 SSE 实测 uvicorn 通过。

## Phase B.1 边界（历史）

本阶段只建立**骨架**，不含真实 agent 编排：

- `service.py`：FastAPI（端口 **8322**），4 个端点返回 stub：
  - `POST /agent/dev` → `{"job_id": uuid, "status": "stub"}`
  - `POST /agent/debug` → `{"job_id": uuid, "status": "stub"}`
  - `POST /agent/review` → `{"job_id": uuid, "status": "stub"}`
  - `GET /agent/{job_id}/events` → SSE 流（骨架：回放连接 + stub 状态后结束）
- `clients/llm.py`：Pydantic AI 门面（model `qwen3.7-flash`，`enable_thinking=False`）。
- `clients/gateway.py`：HTTP → MATRag gateway MCP；`ALLOWED_TOOLS` 白名单 +
  `DENIED_TOOLS` 黑名单双轨。
- `clients/atestudio.py`：HTTP → ATEStudio（长期 JWT）；端点白名单。
- `tools/{read_bundle,plan,derive}.py`：纯函数工具骨架。
- `skills/*.skill.md` / `agents/`：占位，待后续 Phase 填充。

**红线**：不代签；不写 DB；工具只走白名单（`ALLOWED_TOOLS`）；fail-fast，不静默降级。

## 后续 Phase 引用

| Phase | 内容 | 主要文件 |
|---|---|---|
| **B.2** | ATEStudio `aterag_agent.py` langchain → Pydantic AI（D8a，与 B.1 并行，**在 ATEStudio 仓**） | ATEStudio `services/aterag_agent.py` |
| **B.3** | `dev_agent`：自然语言 → bundle draft + 三类解读（pending/gap/warnings，D16） | `agents/dev_agent.py`、`tools/propose_test.py`、`skills/test-dev.skill.md` |
| **B.4** | `debug_agent`：失败 → Top-3 根因 + 建议（S4：30 样本 ≥70%） | `agents/debug_agent.py`、`tools/query_exec.py`、`skills/debug-cycle.skill.md` |
| **B.5** | `dev_scripts_agent`（吸收 HT-A）：pytest 脚本生成 + prd-gen skill | `agents/dev_scripts_agent.py`、`skills/{dev-scripts,prd-gen}.skill.md` |
| **C.1** | 跨仓知识回链（经人工闸门） | MATRag `gateway/tools.py:add_knowledge`（写工具，**不在本仓**） |

参考：`MATRag/docs/superpowers/specs/2026-10-18-architecture-upgrade-design.md` §4.1/§5、
`MATRag/docs/superpowers/plans/2026-10-18-architecture-upgrade-impl-plan.md` §B.1。

## 环境变量

前缀 `MATRAG_AGENT_`（`.env` 亦可）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MATRAG_AGENT_LLM_BASE` / `_API_KEY` / `_MODEL` | 空 / 空 / `qwen3.7-flash` | LLM 端点（OpenAI 兼容） |
| `MATRAG_AGENT_GATEWAY_URL` | `http://127.0.0.1:8080/mcp` | MATRag gateway MCP |
| `MATRAG_AGENT_ATESTUDIO_BASE_URL` | `http://127.0.0.1:8000` | ATEStudio |
| `MATRAG_AGENT_MATRAG_ROOT` | 兄弟目录 `../MATRag` | bundle 导出根 |
| `MATRAG_AGENT_SERVICE_PORT` | `8322` | 服务端口 |

另：`ATESTUDIO_JWT_TOKEN`（**无前缀**，Phase 0 D14 产出的长期 JWT）。

## 运行与测试

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m uvicorn matrag_agent.service:app --port 8322
```
