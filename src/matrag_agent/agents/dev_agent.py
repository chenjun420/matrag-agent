"""dev_agent（Phase B.3）—— 自然语言需求 → bundle draft → plan 三类分离解读。

D16 语义修正：ATEStudio ``/aterag/plan`` 的产出分三类**分别**返回，绝不合并、绝不代签：

- ``pending_signoffs``：未签字的 draft 条件，走 ``POST /api/v1/knowledge/conditions/approve``
  （由人工签字；Agent 不代签）。
- ``gaps``：缺判据 / 缺 binding 的条件，建议补 ``test_method_bindings.yaml``。
- ``warnings``：planner 标注的资源冲突 / 占位限制。

编排为确定性顺序（便离线测试与审计）：``read_bundle`` → ``propose_test`` → ``plan``
→ LLM 综合解读。工具调用前一律经 :func:`check_tool` 白名单闸门；草案 ``status="draft"``，
不写库、不代签（跨仓/落库走 B.4 或人工）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from matrag_agent.tools.plan import plan as plan_tool
from matrag_agent.tools.propose_test import propose_test
from matrag_agent.tools.read_bundle import read_bundle

logger = logging.getLogger(__name__)

#: dev_agent 使用的 LLM 模型（与 matrag-agent LLM 门面默认一致）。
DEV_AGENT_MODEL = "qwen3.7-flash"

#: 允许 dev_agent 调用的工具白名单（Tool name → 用途）。
ALLOWED_TOOLS = frozenset({"read_bundle", "plan", "propose_test"})

#: 被显式拒绝的工具（工具名 → 理由）；与白名单互斥，命中即 fail-fast。
#: 红线：不写库、不代签、不触发跨仓写 / 管理端点。
DENIED_TOOLS: dict[str, str] = {
    "import_bundle": "写 ATEStudio（草案提交由 B.4 / 人工，Agent 不直调）",
    "approve_condition": "代签红线：条件签字必须人工（POST /knowledge/conditions/approve）",
    "ingest_document": "写 MATRag 型号 KB",
    "build_domain_kb": "写 MATRag 共享 KB",
    "add_knowledge": "写公共知识（Phase C.1 经人工闸门）",
}

#: dev_agent 系统提示词（真实部署构造 Pydantic AI Agent 时使用）。
SYSTEM_PROMPT = (
    "你是电源产测软件开发助手（dev_agent）。工作流：读取 MATRag 导出的 bundle → "
    "用 LLM 提案 draft 条件（数值必见原文，status=draft）→ 调 ATEStudio /aterag/plan → "
    "将结果分三类解读：pending（待人工签字）/ gaps（缺判据，需补 binding 表）/ warnings"
    "（planner 警告）。铁律：不代签、不写库、不改动数据；只调用白名单工具；数值必须"
    "能追溯到原文。"
)


class ToolNotAllowedError(PermissionError):
    """工具不在 dev_agent 白名单（或命中黑名单）—— 拒绝执行。"""


@dataclass
class DevAgentResult:
    """dev_agent 运行结果（D16：pending / gap / warnings 三类分离）。

    Attributes:
        bundle_draft: LLM 提案并归一后的 bundle draft（``status="draft"``）。
        pending_signoffs: 未签字 draft 条件（待人工签字）。
        gaps: 无判据 / 缺 binding 的条件（需补 ``test_method_bindings.yaml``）。
        warnings: planner 警告（资源冲突 / 占位限制）。
        llm_interpretation: LLM 给出的综合建议文本。
    """

    bundle_draft: dict[str, Any] = field(default_factory=dict)
    pending_signoffs: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    llm_interpretation: str = ""


def check_tool(name: str) -> None:
    """校验工具名；不在白名单或命中黑名单则抛 :class:`ToolNotAllowedError`。

    沿用 ``clients/gateway.py`` 双轨模式（白名单 + 显式黑名单）。
    """
    if name in DENIED_TOOLS:
        raise ToolNotAllowedError(f"工具 {name!r} 被显式拒绝：{DENIED_TOOLS[name]}")
    if name not in ALLOWED_TOOLS:
        raise ToolNotAllowedError(
            f"工具 {name!r} 不在 dev_agent 白名单；允许: {sorted(ALLOWED_TOOLS)}"
        )


def _record(trace: list[str] | None, name: str) -> None:
    """记录一次工具调用（顺序可观测，供审计 / 测试）。"""
    if trace is not None:
        trace.append(name)


def _norm_items(items: Any, *, kind: str) -> list[dict[str, Any]]:
    """把 plan 的某一类输出归一为 ``list[dict]``。

    - dict 原样保留；
    - 字符串 / 其他标量包装为 ``{"kind": kind, "detail": str}``（留痕而非丢弃）。
    """
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(dict(item))
        else:
            out.append({"kind": kind, "detail": str(item)})
    return out


def _gather_rag(
    gateway_client: Any, product_code: str, user_query: str
) -> dict[str, Any]:
    """经 MATRag gateway 只读检索补充上下文（失败降级但留痕，不静默）。

    Returns:
        ``{"status": "ok"|"degraded"|"skipped", "data": ...}``。
    """
    if gateway_client is None:
        return {"status": "skipped", "reason": "gateway_client 未提供"}
    try:
        data = gateway_client.call_tool(
            "search_requirements", product_code=product_code, query=user_query
        )
        return {"status": "ok", "data": data}
    except Exception as exc:  # 检索失败不阻断提案（降级留痕，绝不吞错）
        logger.warning("RAG 检索降级：%s", exc)
        return {"status": "degraded", "reason": f"{type(exc).__name__}: {exc}"}


def _interpret(
    llm: Any,
    user_query: str,
    draft: dict[str, Any],
    pending: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    rag: dict[str, Any],
) -> str:
    """LLM 综合解读三类结果；LLM 失败时返回带标记的 fallback 文本（留痕）。"""
    payload = {
        "user_query": user_query,
        "draft_requirements": len(draft.get("requirements") or []),
        "draft_review": draft.get("review") or [],
        "pending": pending,
        "gaps": gaps,
        "warnings": warnings,
        "rag_status": rag.get("status"),
    }
    system = (
        "你是产测评审助手。基于给定的结构化结果，用中文给出简洁的综合建议："
        "分别说明待签字（pending）、需补判据（gap）、planner 警告（warnings）"
        "各意味着什么、下一步应做什么。不得编造未在输入中出现的事实。"
    )
    try:
        return str(llm.complete(system, json.dumps(payload, ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001 - 解读失败降级为结构化文本，留痕
        return (
            f"[解读不可用] LLM 调用失败：{type(exc).__name__}: {exc}；"
            "已按结构化三类（pending / gap / warnings）输出，供人工判断。"
        )


async def run_dev_agent(
    product_code: str,
    user_query: str,
    llm: Any,
    gateway_client: Any,
    atestudio_client: Any,
    *,
    exports_dir: Any = None,
    trace: list[str] | None = None,
) -> DevAgentResult:
    """dev_agent 入口：自然语言需求 → bundle draft → plan 三类分离解读。

    Args:
        product_code: 产品编码（决定读取哪个 bundle）。
        user_query: 用户自然语言需求。
        llm: LLM 门面（``complete_json`` / ``complete``）。
        gateway_client: MATRag gateway 客户端（只读检索；可为 None 或 stub）。
        atestudio_client: ATEStudio 客户端（``plan_review``；测试注入 stub）。
        exports_dir: bundle 导出目录覆盖（测试注入 ``tmp_path``）。
        trace: 可选调用记录列表；按顺序追加工具名（``read_bundle``/``propose_test``/``plan``）。

    Returns:
        :class:`DevAgentResult`；draft 条件 ``status="draft"``，未签字、未落库。

    Raises:
        FileNotFoundError: bundle 文件不存在（fail-fast）。
        ToolNotAllowedError: 试图调用白名单外工具。
    """
    check_tool("read_bundle")
    _record(trace, "read_bundle")
    bundle = read_bundle(product_code, exports_dir=exports_dir)

    rag = _gather_rag(gateway_client, product_code, user_query)

    bundle_text = json.dumps(bundle, ensure_ascii=False)
    check_tool("propose_test")
    _record(trace, "propose_test")
    draft = await propose_test(llm, bundle_text, product_code=product_code)

    check_tool("plan")
    _record(trace, "plan")
    plan_result = plan_tool(draft, client=atestudio_client)

    pending = _norm_items(plan_result.get("plan_pending"), kind="pending")
    gaps = _norm_items(plan_result.get("plan_gaps"), kind="gap")
    warnings = _norm_items(plan_result.get("warnings"), kind="warning")

    interpretation = _interpret(llm, user_query, draft, pending, gaps, warnings, rag)

    return DevAgentResult(
        bundle_draft=draft,
        pending_signoffs=pending,
        gaps=gaps,
        warnings=warnings,
        llm_interpretation=interpretation,
    )


def build_dev_agent(
    llm: Any,
    *,
    atestudio_client: Any = None,
    exports_dir: Any = None,
) -> Any:
    """构造真实部署用的 Pydantic AI Agent（model=``DEV_AGENT_MODEL``）+ 白名单工具。

    Agent 循环仅在真实 LLM 端点可用时使用；离线测试走 :func:`run_dev_agent`
    的确定性编排。模型对象来自 LLM 门面（``llm.build_model()``），构造不触网。

    Raises:
        LLMNotConfiguredError: LLM 未配置（fail-closed）。
    """
    from pydantic_ai import Agent

    model = llm.build_model()

    def tool_read_bundle(product_code: str) -> dict[str, Any]:
        """读取 MATRag 导出的 bundle。"""
        return read_bundle(product_code, exports_dir=exports_dir)

    def tool_plan(bundle: dict[str, Any]) -> dict[str, Any]:
        """调 ATEStudio ``/aterag/plan`` 生成 plan。"""
        return plan_tool(bundle, client=atestudio_client)

    async def tool_propose_test(bundle_text: str) -> dict[str, Any]:
        """LLM 提案 bundle draft（status=draft）。"""
        return await propose_test(llm, bundle_text)

    return Agent(model, system_prompt=SYSTEM_PROMPT, tools=[tool_read_bundle, tool_plan, tool_propose_test])
