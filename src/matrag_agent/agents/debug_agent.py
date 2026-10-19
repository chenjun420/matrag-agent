"""debug_agent（Phase B.4）—— 执行失败 → 审计 → RAG → LLM 综合 Top-3 根因 + 建议。

工作流（确定性顺序，便离线测试与审计）：``query_exec``（调 ATEStudio
``failure_indexer.audit_report``）→ ``rag_search``（调 MATRag gateway ``/ask`` 检索
已知 root cause patterns）→ LLM 综合 Top-3 根因 + 每条推荐下一步。工具调用前一律经
:func:`check_tool` 白名单闸门。

S4 指标（spec §5 B.4）：样本 ≥30；「命中」= top-3 含真实根因；阈值 ≥70%。本模块只做
**只读诊断**：不写 DB、不落 FMEA 知识库（``add_knowledge`` 走 C.1 人工闸门）、不代签。

降级纪律：RAG 失败 / LLM 失败一律**留痕**（``DebugAgentResult.degradations``），不静默
吞错；根因 + 建议为空时 ``confidence="low"``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from matrag_agent.tools.query_exec import query_exec as query_exec_tool
from matrag_agent.tools.rag_search import rag_search as rag_search_tool

logger = logging.getLogger(__name__)

#: debug_agent 使用的 LLM 模型（与 matrag-agent LLM 门面默认一致）。
DEBUG_AGENT_MODEL = "qwen3.7-flash"

#: 允许 debug_agent 调用的工具白名单（Tool name → 用途）。
ALLOWED_TOOLS = frozenset({"query_exec", "rag_search"})

#: 被显式拒绝的工具（工具名 → 理由）；与白名单互斥，命中即 fail-fast。
#: 红线：不写库、不代签、不落 FMEA 知识库（C.1 阶段经人工闸门）、不触发跨仓写。
DENIED_TOOLS: dict[str, str] = {
    "import_bundle": "写 ATEStudio（bundle 提交由人工，Agent 不直调）",
    "approve_condition": "代签红线：条件签字必须人工（POST /knowledge/conditions/approve）",
    "ingest_document": "写 MATRag 型号 KB",
    "build_domain_kb": "写 MATRag 共享 KB",
    "add_knowledge": "写公共知识 / 落 FMEA 知识库（Phase C.1 经人工闸门，不由 Agent 直调）",
}

#: debug_agent 系统提示词（真实部署构造 Pydantic AI Agent 时使用）。
SYSTEM_PROMPT = (
    "你是电源产测调测诊断助手（debug_agent）。工作流：读 execution 失败链路审计 → "
    "检索已知 root cause patterns → 综合给出可能根因 Top-3（按可能性降序）+ 每条对应的"
    "推荐下一步（重测 / 调整夹具 / 调整激励 / 走工程评审）。铁律：只调用白名单工具；"
    "结论必须标注证据来源；不写库、不代签、不落知识库；输入中未出现的证据不得编造。"
)

#: 置信度阈值：top 根因 likelihood ≥ 高阈值 → high；≥ 中阈值 → medium；否则 low。
CONFIDENCE_HIGH_MIN = 0.7
CONFIDENCE_MEDIUM_MIN = 0.4

#: Top-N 根因上限（S4：top-3）。
TOP_N = 3

#: 综合根因的 JSON schema 提示。
_SCHEMA_HINT = (
    '{"root_causes":[{"description":"<可能根因>","evidence":["<证据来源>"],'
    '"likelihood":0.0}],"recommendations":["<推荐下一步>"]}'
)


class ToolNotAllowedError(PermissionError):
    """工具不在 debug_agent 白名单（或命中黑名单）—— 拒绝执行。"""


@dataclass
class RootCause:
    """一条可能根因。

    Attributes:
        description: 根因描述。
        evidence: 证据来源列表（来自审计 / RAG 上下文，须可追溯）。
        likelihood: 可能性（0~1，越大越可能）。
    """

    description: str
    evidence: list[str] = field(default_factory=list)
    likelihood: float = 0.0


@dataclass
class DebugAgentResult:
    """debug_agent 运行结果（Top-3 根因 + 一一对应的推荐下一步）。

    Attributes:
        execution_id: 被诊断的执行记录 id。
        audit_summary: ATEStudio ``failure_indexer.audit_report`` 原始审计报告。
        rag_context: MATRag gateway ``/ask`` 检索到的 chunk（``text`` / ``source`` /
            ``score``）。
        root_causes: Top-3 根因（按可能性降序）。
        recommendations: 推荐下一步（与 ``root_causes`` 一一对应）。
        confidence: ``high`` / ``medium`` / ``low``（由 top 根因 likelihood 决定）。
        degradations: 降级留痕（RAG / LLM 失败记录；空表示全链路正常）。
    """

    execution_id: str
    audit_summary: dict = field(default_factory=dict)
    rag_context: list[dict] = field(default_factory=list)
    root_causes: list[RootCause] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    confidence: str = "low"
    degradations: list[dict] = field(default_factory=list)


def check_tool(name: str) -> None:
    """校验工具名；不在白名单或命中黑名单则抛 :class:`ToolNotAllowedError`。

    沿用 ``clients/gateway.py`` 双轨模式（白名单 + 显式黑名单）。
    """
    if name in DENIED_TOOLS:
        raise ToolNotAllowedError(f"工具 {name!r} 被显式拒绝：{DENIED_TOOLS[name]}")
    if name not in ALLOWED_TOOLS:
        raise ToolNotAllowedError(
            f"工具 {name!r} 不在 debug_agent 白名单；允许: {sorted(ALLOWED_TOOLS)}"
        )


def _record(trace: list[str] | None, name: str) -> None:
    """记录一次工具调用（顺序可观测，供审计 / 测试）。"""
    if trace is not None:
        trace.append(name)


def _clamp01(value: Any) -> float:
    """把 likelihood 归一到 ``[0, 1]``；非数值退化为 ``0.0``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _normalize_root_causes(raw: Any) -> list[RootCause]:
    """归一 LLM 输出的根因列表：跳过无描述项，按 likelihood 降序取 Top-3。"""
    if not isinstance(raw, list):
        return []
    causes: list[RootCause] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        evidence_raw = item.get("evidence")
        evidence = (
            [str(e) for e in evidence_raw] if isinstance(evidence_raw, list) else []
        )
        causes.append(
            RootCause(
                description=description,
                evidence=evidence,
                likelihood=_clamp01(item.get("likelihood")),
            )
        )
    causes.sort(key=lambda cause: cause.likelihood, reverse=True)
    return causes[:TOP_N]


def _normalize_recommendations(raw: Any, *, count: int) -> list[str]:
    """归一推荐文本，并与根因数对齐（超出截断；不足按缺失留痕由调用方补齐）。"""
    if not isinstance(raw, list) or count <= 0:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            text = str(
                item.get("action") or item.get("text") or item.get("detail") or ""
            ).strip()
            if text:
                out.append(text)
    return out[:count]


def _confidence_for(root_causes: list[RootCause]) -> str:
    """由 top 根因 likelihood 决定置信度：high / medium / low。"""
    if not root_causes:
        return "low"
    top = root_causes[0].likelihood
    if top >= CONFIDENCE_HIGH_MIN:
        return "high"
    if top >= CONFIDENCE_MEDIUM_MIN:
        return "medium"
    return "low"


def _default_rag_query(
    audit_summary: dict, failed_step_id: str | None
) -> str:
    """由审计报告 + 失败 step 拼默认检索串（失败步骤名/原因 + 状态 + 需求编号）。"""
    parts: list[str] = []
    if failed_step_id:
        parts.append(str(failed_step_id))
    failed_steps = audit_summary.get("failed_steps")
    if isinstance(failed_steps, list):
        for step in failed_steps:
            if not isinstance(step, dict):
                continue
            if step.get("step_id"):
                parts.append(str(step["step_id"]))
            if step.get("reason"):
                parts.append(str(step["reason"]))
    if audit_summary.get("execution_status"):
        parts.append(str(audit_summary["execution_status"]))
    codes = audit_summary.get("used_requirement_codes")
    if isinstance(codes, list):
        parts.extend(str(code) for code in codes if code)
    query = " ".join(part for part in parts if part).strip()
    if query:
        return query
    return f"execution {audit_summary.get('execution_id', '')} failure root cause"


async def _gather_rag(
    gateway_client: Any, query: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """经 gateway 只读检索补充上下文（失败降级但留痕，不静默吞错）。

    Returns:
        ``(chunks, degradation)``：成功时 ``degradation`` 为 ``None``。
    """
    try:
        chunks = await rag_search_tool(gateway_client, query)
        return chunks, None
    except Exception as exc:  # 检索失败不阻断诊断（降级留痕，绝不吞错）
        logger.warning("RAG 检索降级：%s", exc)
        return [], {"kind": "rag_degraded", "detail": f"{type(exc).__name__}: {exc}"}


def _synthesize(
    llm: Any,
    execution_id: str,
    audit_summary: dict,
    rag_context: list[dict[str, Any]],
    device_readings: dict[str, Any] | None,
    plan_yaml: str | None,
) -> tuple[list[RootCause], list[str], str, dict[str, Any] | None]:
    """LLM 综合 Top-3 根因 + 推荐下一步；LLM 失败 → 空列表 + low（留痕）。

    Returns:
        ``(root_causes, recommendations, confidence, degradation)``。
    """
    payload = {
        "execution_id": execution_id,
        "audit_summary": audit_summary,
        "device_readings": device_readings or {},
        "plan_yaml": (plan_yaml or "")[:4000],
        "rag_context": rag_context,
    }
    system = (
        "你是电源产测调测诊断助手。基于 execution 失败链路审计、设备读数与检索到的"
        "已知 root cause patterns，给出可能根因 Top-3（按可能性降序，每条附证据来源）"
        "与一一对应的推荐下一步（重测 / 调整夹具 / 调整激励 / 走工程评审）。"
        "不得编造输入中未出现的证据或事实。"
    )
    try:
        raw = llm.complete_json(
            system, json.dumps(payload, ensure_ascii=False), schema_hint=_SCHEMA_HINT
        )
    except Exception as exc:  # LLM / 网络 / 解析失败 → fallback（留痕，不静默）
        logger.warning("LLM 综合失败，降级为空根因：%s", exc)
        return [], [], "low", {
            "kind": "debug_llm_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    if not isinstance(raw, dict):
        return [], [], "low", {
            "kind": "debug_llm_invalid",
            "detail": f"LLM 输出非对象: {type(raw).__name__}",
        }

    root_causes = _normalize_root_causes(raw.get("root_causes"))
    recommendations = _normalize_recommendations(
        raw.get("recommendations"), count=len(root_causes)
    )
    degradation: dict[str, Any] | None = None
    # 一一对应不变量：建议不足时补确定性占位并留痕（不编造事实，只补可执行动作）。
    if 0 < len(recommendations) < len(root_causes):
        shortfall = len(root_causes) - len(recommendations)
        recommendations = recommendations + [
            "建议人工复核该根因并安排复现确认与工程评审"
        ] * shortfall
        degradation = {
            "kind": "debug_recommendation_padded",
            "detail": f"LLM 建议少于根因数，补 {shortfall} 条占位建议",
        }
    return root_causes, recommendations, _confidence_for(root_causes), degradation


async def run_debug_agent(
    execution_id: str,
    llm: Any,
    gateway_client: Any,
    atestudio_client: Any,
    *,
    rag_query: str | None = None,
    failed_step_id: str | None = None,
    device_readings: dict[str, Any] | None = None,
    plan_yaml: str | None = None,
    trace: list[str] | None = None,
) -> DebugAgentResult:
    """debug_agent 入口：execution 失败 → 审计 → RAG → LLM 综合 Top-3 根因 + 建议。

    Args:
        execution_id: 执行记录 id（= run_id）。
        llm: LLM 门面（``complete_json``）。
        gateway_client: MATRag gateway 客户端（只读检索；可为 None → 降级留痕）。
        atestudio_client: ATEStudio 客户端（``audit_report``；测试注入 stub）。
        rag_query: 检索串覆盖；缺省由审计报告 + 失败 step 拼装。
        failed_step_id: 失败 step id（参与默认检索串；可选）。
        device_readings: 设备读数（可选，参与 LLM 综合）。
        plan_yaml: plan_yaml 摘要（可选，参与 LLM 综合）。
        trace: 可选调用记录列表；按顺序追加工具名（``query_exec``/``rag_search``）。

    Returns:
        :class:`DebugAgentResult`：Top-3 根因 + 一一对应的推荐下一步 + 置信度；
        降级情况记录在 ``degradations``。

    Raises:
        ToolNotAllowedError: 试图调用白名单外工具。
        QueryExecError: 审计查询失败（fail-fast；不降级为假审计）。
    """
    degradations: list[dict[str, Any]] = []

    check_tool("query_exec")
    _record(trace, "query_exec")
    audit_summary = await query_exec_tool(atestudio_client, execution_id)

    query = rag_query or _default_rag_query(audit_summary, failed_step_id)
    check_tool("rag_search")
    _record(trace, "rag_search")
    rag_context, rag_degradation = await _gather_rag(gateway_client, query)
    if rag_degradation is not None:
        degradations.append(rag_degradation)

    root_causes, recommendations, confidence, llm_degradation = _synthesize(
        llm, execution_id, audit_summary, rag_context, device_readings, plan_yaml
    )
    if llm_degradation is not None:
        degradations.append(llm_degradation)

    return DebugAgentResult(
        execution_id=execution_id,
        audit_summary=audit_summary,
        rag_context=rag_context,
        root_causes=root_causes,
        recommendations=recommendations,
        confidence=confidence,
        degradations=degradations,
    )


def build_debug_agent(
    llm: Any,
    *,
    atestudio_client: Any = None,
    gateway_client: Any = None,
) -> Any:
    """构造真实部署用的 Pydantic AI Agent（model=``DEBUG_AGENT_MODEL``）+ 白名单工具。

    Agent 循环仅在真实 LLM 端点可用时使用；离线测试走 :func:`run_debug_agent`
    的确定性编排。模型对象来自 LLM 门面（``llm.build_model()``），构造不触网。

    Raises:
        LLMNotConfiguredError: LLM 未配置（fail-closed）。
    """
    from pydantic_ai import Agent

    model = llm.build_model()

    async def tool_query_exec(execution_id: str) -> dict[str, Any]:
        """调 ATEStudio 取 execution 失败链路审计报告（只读）。"""
        return await query_exec_tool(atestudio_client, execution_id)

    async def tool_rag_search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """经 MATRag gateway ``/ask`` 检索已知 root cause patterns（只读）。"""
        return await rag_search_tool(gateway_client, query, top_k=top_k)

    return Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        tools=[tool_query_exec, tool_rag_search],
    )
