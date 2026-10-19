"""debug_agent（Phase B.4）单元测试：全离线（stub LLM / gateway / ATEStudio）。

覆盖：Top-3 根因结构、排序、confidence 映射（high/medium/low）、LLM 失败 fallback、
工具调用顺序、白名单闸门、不同 execution 状态、RAG 降级留痕、query_exec / rag_search
归一，以及 Pydantic AI Agent 构造。
"""

from __future__ import annotations

from typing import Any

import pytest

from matrag_agent.agents import DebugAgentResult, RootCause, run_debug_agent
from matrag_agent.agents.debug_agent import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    ToolNotAllowedError,
    build_debug_agent,
    check_tool,
)
from matrag_agent.clients.llm import LLMClient, LLMNotConfiguredError
from matrag_agent.tools.query_exec import QueryExecError, query_exec
from matrag_agent.tools.rag_search import RagSearchError, rag_search


# ------------------------------------------------------------------ stubs ----
class StubLLM:
    """离线 LLM stub：回放 ``complete_json``；可注入异常。"""

    def __init__(
        self,
        *,
        json_responses: list[dict[str, Any]] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self._json_responses = list(json_responses or [])
        self._json_error = json_error
        self.json_calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        schema_hint: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.json_calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self._json_error is not None:
            raise self._json_error
        if self._json_responses:
            return self._json_responses.pop(0)
        return {"root_causes": [], "recommendations": []}


class StubAteStudio:
    """离线 ATEStudio stub：记录 execution_id，回放固定审计报告。"""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        self.calls: list[str] = []

    def audit_report(self, execution_id: str) -> dict[str, Any]:
        self.calls.append(execution_id)
        return self.report


class StubGateway:
    """离线 gateway stub：记录工具调用，返回固定 payload 或抛错。"""

    def __init__(
        self, payload: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.payload


# ------------------------------------------------------------------ 数据 ----
AUDIT: dict[str, Any] = {
    "execution_id": "exec-123",
    "execution_status": "failed",
    "used_requirement_codes": ["REQ-1"],
    "measured_conditions": [],
    "failed_steps": [{"step_id": "s3", "reason": "output 53.1V out of range"}],
    "pending_signoffs": [],
}

#: LLM 综合输出（3 根因 + 3 建议）。
THREE: dict[str, Any] = {
    "root_causes": [
        {"description": "输出电容容量不足", "evidence": ["rag:RH-1"], "likelihood": 0.82},
        {"description": "夹具接触电阻偏高", "evidence": ["audit:failed_steps"], "likelihood": 0.55},
        {"description": "输入激励频率偏差", "evidence": ["plan_yaml"], "likelihood": 0.31},
    ],
    "recommendations": ["调整夹具", "重测", "调整激励"],
}

#: gateway ``/ask`` 返回（chunk 形态）。
GATEWAY_OK: dict[str, Any] = {
    "chunks": [{"text": "历史:输出偏低根因电容", "source": "fmea:PA601", "score": 0.9}]
}


async def _run(
    *,
    llm: StubLLM | None = None,
    gateway: StubGateway | None = None,
    audit: dict[str, Any] | None = None,
    trace: list[str] | None = None,
    execution_id: str = "exec-123",
    **kwargs: Any,
) -> DebugAgentResult:
    """端到端跑 run_debug_agent（离线）。"""
    _llm = llm if llm is not None else StubLLM(json_responses=[THREE])
    _gw = gateway if gateway is not None else StubGateway(GATEWAY_OK)
    _at = StubAteStudio(audit if audit is not None else AUDIT)
    return await run_debug_agent(
        execution_id, _llm, _gw, _at, trace=trace, **kwargs
    )


# ------------------------------------------------------------ 白名单闸门 ----
def test_allowed_tools_pinned() -> None:
    """CI pin：debug_agent 工具白名单固定。"""
    assert ALLOWED_TOOLS == {"query_exec", "rag_search"}


def test_denied_tools_pinned() -> None:
    """CI pin：debug_agent 拒绝清单固定（写 / 代签 / 落知识库）。"""
    assert set(DENIED_TOOLS) == {
        "import_bundle",
        "approve_condition",
        "ingest_document",
        "build_domain_kb",
        "add_knowledge",
    }


def test_allowed_denied_disjoint() -> None:
    assert ALLOWED_TOOLS.isdisjoint(DENIED_TOOLS)


def test_check_tool_rejects_denied() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("ingest_document")


def test_check_tool_rejects_add_knowledge() -> None:
    """落 FMEA 知识库（C.1 人工闸门）被显式拒绝。"""
    with pytest.raises(ToolNotAllowedError):
        check_tool("add_knowledge")


def test_check_tool_rejects_unknown() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("definitely_not_a_tool")


def test_check_tool_allows_whitelisted() -> None:
    for name in ALLOWED_TOOLS:
        check_tool(name)  # 不抛


# ------------------------------------------------------------ query_exec ----
@pytest.mark.asyncio
async def test_query_exec_returns_audit() -> None:
    at = StubAteStudio(AUDIT)
    out = await query_exec(at, "exec-123")
    assert out["execution_status"] == "failed"
    assert at.calls == ["exec-123"]


@pytest.mark.asyncio
async def test_query_exec_requires_client() -> None:
    with pytest.raises(QueryExecError):
        await query_exec(None, "exec-123")


@pytest.mark.asyncio
async def test_query_exec_rejects_empty_id() -> None:
    with pytest.raises(QueryExecError):
        await query_exec(StubAteStudio(AUDIT), "   ")


# ------------------------------------------------------------ rag_search ----
@pytest.mark.asyncio
async def test_rag_search_returns_chunks() -> None:
    gw = StubGateway(GATEWAY_OK)
    chunks = await rag_search(gw, "根因检索")
    assert chunks == [
        {
            "text": "历史:输出偏低根因电容",
            "source": "fmea:PA601",
            "score": 0.9,
            "chunk_id": "",
            "doc_id": "",
        }
    ]
    assert gw.calls[0][0] == "ask"
    assert gw.calls[0][1] == {"query": "根因检索", "top_k": 5}


@pytest.mark.asyncio
async def test_rag_search_normalizes_search_context_hits() -> None:
    """兼容 search_context 形态的 hits（text + anchor + score）。"""
    gw = StubGateway({"hits": [{"text": "证据正文", "score": 0.42, "anchor": {"doc_id": "DOC-1"}}]})
    chunks = await rag_search(gw, "q")
    assert chunks[0]["text"] == "证据正文"
    assert chunks[0]["source"] == "DOC-1"
    assert chunks[0]["score"] == 0.42


@pytest.mark.asyncio
async def test_rag_search_reads_text_from_ask_citations() -> None:
    """``ask`` 形态的 citations（G2 起带 text 原文片段）: 优先取真原文, 不退回 section_path。"""
    gw = StubGateway(
        {
            "citations": [
                {
                    "marker": "[1]",
                    "doc_id": "PA601-D54A",
                    "section_path": "5 > 输出特性",
                    "chunk_id": "PA601-D54A:0007",
                    "score": 0.0322,
                    "text": "输出电压限值 5V。",
                }
            ]
        }
    )
    chunks = await rag_search(gw, "q")
    assert chunks[0]["text"] == "输出电压限值 5V。"
    assert chunks[0]["source"] == "PA601-D54A"
    assert chunks[0]["chunk_id"] == "PA601-D54A:0007"
    assert chunks[0]["score"] == 0.0322


@pytest.mark.asyncio
async def test_rag_search_error_payload_raises() -> None:
    with pytest.raises(RagSearchError):
        await rag_search(StubGateway({"error": "empty_query"}), "q")


@pytest.mark.asyncio
async def test_rag_search_requires_client() -> None:
    with pytest.raises(RagSearchError):
        await rag_search(None, "q")


# ---------------------------------------------------- 端到端 Top-3 根因 ----
@pytest.mark.asyncio
async def test_run_debug_agent_top3_end_to_end() -> None:
    res = await _run()
    assert isinstance(res, DebugAgentResult)
    assert res.execution_id == "exec-123"
    assert res.audit_summary["execution_status"] == "failed"
    assert len(res.root_causes) == 3
    assert len(res.recommendations) == 3
    assert all(isinstance(rc, RootCause) for rc in res.root_causes)
    top = res.root_causes[0]
    assert top.description == "输出电容容量不足"
    assert top.evidence == ["rag:RH-1"]
    assert top.likelihood == 0.82
    assert res.confidence == "high"
    assert res.rag_context[0]["text"] == "历史:输出偏低根因电容"
    assert res.degradations == []


@pytest.mark.asyncio
async def test_run_debug_agent_sorts_root_causes_desc() -> None:
    llm = StubLLM(
        json_responses=[
            {
                "root_causes": [
                    {"description": "低", "evidence": [], "likelihood": 0.2},
                    {"description": "高", "evidence": [], "likelihood": 0.9},
                    {"description": "中", "evidence": [], "likelihood": 0.6},
                ],
                "recommendations": ["a", "b", "c"],
            }
        ]
    )
    res = await _run(llm=llm)
    assert [rc.description for rc in res.root_causes] == ["高", "中", "低"]
    assert [rc.likelihood for rc in res.root_causes] == [0.9, 0.6, 0.2]


@pytest.mark.asyncio
async def test_run_debug_agent_caps_top3() -> None:
    llm = StubLLM(
        json_responses=[
            {
                "root_causes": [
                    {"description": f"c{i}", "evidence": [], "likelihood": 0.5 - i * 0.05}
                    for i in range(5)
                ],
                "recommendations": ["r0", "r1", "r2", "r3", "r4"],
            }
        ]
    )
    res = await _run(llm=llm)
    assert len(res.root_causes) == 3
    assert len(res.recommendations) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("likelihood", "expected"),
    [(0.9, "high"), (0.7, "high"), (0.5, "medium"), (0.4, "medium"), (0.2, "low")],
)
async def test_confidence_mapping(likelihood: float, expected: str) -> None:
    llm = StubLLM(
        json_responses=[
            {"root_causes": [{"description": "x", "evidence": [], "likelihood": likelihood}],
             "recommendations": ["重测"]}
        ]
    )
    res = await _run(llm=llm)
    assert res.confidence == expected


@pytest.mark.asyncio
async def test_llm_failure_fallback() -> None:
    llm = StubLLM(json_error=RuntimeError("llm down"))
    res = await _run(llm=llm)
    assert res.root_causes == []
    assert res.recommendations == []
    assert res.confidence == "low"
    assert any(d["kind"] == "debug_llm_error" for d in res.degradations)


@pytest.mark.asyncio
async def test_rag_degraded_recorded() -> None:
    """RAG 失败 → 降级留痕，但不阻断 LLM 综合。"""
    gw = StubGateway(error=RuntimeError("gateway down"))
    res = await _run(gateway=gw)
    assert gw.calls  # 确实尝试过检索
    assert res.rag_context == []
    assert any(d["kind"] == "rag_degraded" for d in res.degradations)
    assert len(res.root_causes) == 3  # LLM 仍正常综合


@pytest.mark.asyncio
async def test_run_debug_agent_tool_order() -> None:
    trace: list[str] = []
    llm = StubLLM(json_responses=[THREE])
    await _run(llm=llm, trace=trace)
    assert trace == ["query_exec", "rag_search"]
    assert len(llm.json_calls) == 1  # LLM 在工具之后调用


@pytest.mark.asyncio
async def test_default_rag_query_uses_audit() -> None:
    trace: list[str] = []
    gw = StubGateway(GATEWAY_OK)
    await _run(gateway=gw, trace=trace)
    query = gw.calls[0][1]["query"]
    assert "out of range" in query  # 失败原因进入检索串
    assert "REQ-1" in query


@pytest.mark.asyncio
async def test_rag_query_override() -> None:
    gw = StubGateway(GATEWAY_OK)
    await _run(gateway=gw, rag_query="自定义检索串")
    assert gw.calls[0][1]["query"] == "自定义检索串"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["running", "failed", "passed"])
async def test_execution_states(status: str) -> None:
    audit = dict(AUDIT)
    audit["execution_status"] = status
    res = await _run(audit=audit)
    assert res.audit_summary["execution_status"] == status
    assert len(res.root_causes) == 3


# ------------------------------------------------------------- Pydantic AI ----
def test_build_debug_agent_requires_configured_llm(make_settings) -> None:
    with pytest.raises(LLMNotConfiguredError):
        build_debug_agent(LLMClient(make_settings()))


def test_build_debug_agent_constructs(make_settings) -> None:
    llm = LLMClient(
        make_settings(llm_base="http://127.0.0.1:1/v1", llm_api_key="k", llm_model="qwen3.7-flash")
    )
    agent = build_debug_agent(llm)
    assert type(agent).__name__ == "Agent"
