"""dev_agent（Phase B.3）单元测试：全离线（stub LLM / gateway / ATEStudio）。

覆盖：三类分离解读（pending-only / gap-only / warnings-only / 混合）、bundle draft
结构、fact guard 触发（数值不在原文 → 弃值）、kind 词表外 → 弃、measure 无判据 → 弃、
LLM 失败 → fallback、重试一次、工具调用顺序、端到端 stub。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from matrag_agent.agents import DevAgentResult, run_dev_agent
from matrag_agent.agents.dev_agent import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    ToolNotAllowedError,
    build_dev_agent,
    check_tool,
)
from matrag_agent.clients.llm import LLMClient, LLMNotConfiguredError
from matrag_agent.tools.propose_test import propose_test


# ------------------------------------------------------------------ stubs ----
class StubLLM:
    """离线 LLM stub：按序回放 ``complete_json``；``complete`` 返回固定文本。"""

    def __init__(
        self,
        *,
        json_responses: list[dict[str, Any]] | None = None,
        json_error: Exception | None = None,
        complete_text: str = "建议：先人工签字 pending，再补 gap 的 binding。",
        complete_error: Exception | None = None,
    ) -> None:
        self._json_responses = list(json_responses or [])
        self._json_error = json_error
        self.complete_text = complete_text
        self.complete_error = complete_error
        self.json_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    def complete_json(
        self, system: str, user: str, *, schema_hint: str = "", max_tokens: int | None = None
    ) -> dict[str, Any]:
        self.json_calls.append({"system": system, "user": user, "schema_hint": schema_hint})
        if self._json_error is not None:
            raise self._json_error
        if self._json_responses:
            return self._json_responses.pop(0)
        return {"product_code": "PA601", "requirements": []}

    def complete(
        self, system: str, user: str, *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        self.complete_calls.append({"system": system, "user": user})
        if self.complete_error is not None:
            raise self.complete_error
        return self.complete_text


class StubAteStudio:
    """离线 ATEStudio stub：记录收到的 bundle，回放固定 plan payload。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def plan_review(self, bundle: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(bundle)
        return self.payload


class StubGateway:
    """离线 gateway stub：记录工具调用，返回固定 payload 或抛错。"""

    def __init__(self, payload: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.payload


def _raw(*conditions: dict[str, Any], code: str = "REQ-1", text: str = "输出电压 54V") -> dict[str, Any]:
    return {"product_code": "PA601", "requirements": [{"code": code, "text": text, "conditions": list(conditions)}]}


def _write_bundle(dir_path: Path, code: str, payload: dict[str, Any]) -> None:
    (dir_path / f"{code}.bundle.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def _run(
    tmp_path: Path,
    plan_payload: dict[str, Any],
    *,
    llm: StubLLM | None = None,
    gateway: StubGateway | None = None,
    trace: list[str] | None = None,
    bundle: dict[str, Any] | None = None,
) -> DevAgentResult:
    """端到端跑 run_dev_agent（离线）。"""
    _write_bundle(tmp_path, "PA601", bundle or {"product_code": "PA601", "spec": "输出电压 54V"})
    llm = llm or StubLLM()
    at = StubAteStudio(plan_payload)
    gw = gateway if gateway is not None else StubGateway()
    return await run_dev_agent("PA601", "输出电压规格", llm, gw, at, exports_dir=tmp_path, trace=trace)


# ------------------------------------------------------ propose_test 归一 ----
@pytest.mark.asyncio
async def test_propose_test_draft_structure() -> None:
    cond = {"kind": "output_voltage", "side": "output", "text": "输出电压 54V", "value": {"typ": 54, "unit": "V"}}
    llm = StubLLM(json_responses=[_raw(cond)])
    draft = await propose_test(llm, "输出电压 54V", product_code="PA601")
    assert draft["status"] == "draft"
    assert draft["fallback"] is False
    req = draft["requirements"][0]
    assert req["code"] == "REQ-1"
    cond = req["conditions"][0]
    assert cond["kind"] == "output_voltage"
    assert cond["status"] == "draft"
    assert cond["value"] == {"typ": 54.0, "unit": "V"}
    assert draft["stats"]["conditions_draft"] == 1


@pytest.mark.asyncio
async def test_propose_test_fact_guard_drops_value() -> None:
    """数值不在原文 → 弃值；measure 类弃值后整条弃用。"""
    cond = {"kind": "output_voltage", "side": "output", "text": "输出电压 99V", "value": {"typ": 99, "unit": "V"}}
    llm = StubLLM(json_responses=[_raw(cond)])
    draft = await propose_test(llm, "输出电压 54V", product_code="PA601")
    assert draft["requirements"][0]["conditions"] == []
    assert draft["stats"]["values_dropped"] == 1
    assert draft["stats"]["conditions_dropped"] == 1
    kinds = {r["kind"] for r in draft["review"]}
    assert "propose_value_dropped" in kinds
    assert "propose_measure_no_value" in kinds


@pytest.mark.asyncio
async def test_propose_test_keeps_only_numbers_seen_in_text() -> None:
    """多数值中只有逐字见于原文的保留。"""
    raw = _raw(
        {
            "kind": "output_voltage",
            "side": "output",
            "text": "输出电压 54V",
            "value": {"min": 52, "typ": 54, "max": 56, "unit": "V"},
        }
    )
    draft = await propose_test(StubLLM(json_responses=[raw]), "标称输出电压 54V", product_code="PA601")
    assert draft["requirements"][0]["conditions"][0]["value"] == {"typ": 54.0, "unit": "V"}


@pytest.mark.asyncio
async def test_propose_test_kind_outside_vocab_dropped() -> None:
    raw = _raw({"kind": "bogus_kind", "side": "output", "text": "无关", "value": {"typ": 1}})
    draft = await propose_test(StubLLM(json_responses=[raw]), "无关", product_code="PA601")
    assert draft["requirements"][0]["conditions"] == []
    assert draft["stats"]["kinds_dropped"] == 1
    assert any(r["kind"] == "propose_kind_invalid" for r in draft["review"])


@pytest.mark.asyncio
async def test_propose_test_non_measure_without_value_kept() -> None:
    """非 measure 类无数值仍保留（如 signal_state）。"""
    raw = _raw({"kind": "signal_state", "side": "output", "text": "PG 信号有效"})
    draft = await propose_test(StubLLM(json_responses=[raw]), "PG 信号有效", product_code="PA601")
    cond = draft["requirements"][0]["conditions"][0]
    assert cond["kind"] == "signal_state"
    assert cond["value"] is None


@pytest.mark.asyncio
async def test_propose_test_retries_once_on_bad_schema() -> None:
    llm = StubLLM(json_responses=[{}, _raw({"kind": "signal_state", "side": "output", "text": "PG 有效"})])
    draft = await propose_test(llm, "PG 有效", product_code="PA601")
    assert draft["fallback"] is False
    assert len(llm.json_calls) == 2
    # 第二次带更强 schema hint
    assert llm.json_calls[0]["schema_hint"] != llm.json_calls[1]["schema_hint"]


@pytest.mark.asyncio
async def test_propose_test_llm_exception_fallback() -> None:
    llm = StubLLM(json_error=RuntimeError("endpoint down"))
    draft = await propose_test(llm, "任意原文", product_code="PA601")
    assert draft["fallback"] is True
    assert draft["status"] == "draft"
    assert draft["requirements"] == []
    assert len(llm.json_calls) == 2  # 重试一次后 fallback
    assert any(r["kind"] == "propose_llm_error" for r in draft["review"])


@pytest.mark.asyncio
async def test_propose_test_double_invalid_fallback() -> None:
    llm = StubLLM(json_responses=[{}, {}])
    draft = await propose_test(llm, "任意原文", product_code="PA601")
    assert draft["fallback"] is True
    assert len(llm.json_calls) == 2


# ------------------------------------------------------------ 白名单闸门 ----
def test_allowed_denied_disjoint() -> None:
    assert ALLOWED_TOOLS.isdisjoint(DENIED_TOOLS)
    assert ALLOWED_TOOLS == {"read_bundle", "plan", "propose_test"}


def test_check_tool_rejects_denied() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("approve_condition")


def test_check_tool_rejects_unknown() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("definitely_not_a_tool")


def test_check_tool_allows_whitelisted() -> None:
    for name in ALLOWED_TOOLS:
        check_tool(name)  # 不抛


# ----------------------------------------------------- run_dev_agent 三类 ----
@pytest.mark.asyncio
async def test_run_dev_agent_pending_only(tmp_path: Path) -> None:
    res = await _run(tmp_path, {"pending": [{"cond": "c1"}], "gaps": [], "warnings": []})
    assert res.pending_signoffs == [{"cond": "c1"}]
    assert res.gaps == []
    assert res.warnings == []


@pytest.mark.asyncio
async def test_run_dev_agent_gap_only(tmp_path: Path) -> None:
    res = await _run(tmp_path, {"pending": [], "gaps": [{"kind": "no_binding"}], "warnings": []})
    assert res.pending_signoffs == []
    assert res.gaps == [{"kind": "no_binding"}]
    assert res.warnings == []


@pytest.mark.asyncio
async def test_run_dev_agent_warnings_only(tmp_path: Path) -> None:
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": ["资源冲突: 通道 1"]})
    assert res.pending_signoffs == []
    assert res.gaps == []
    assert res.warnings == [{"kind": "warning", "detail": "资源冲突: 通道 1"}]


@pytest.mark.asyncio
async def test_run_dev_agent_mixed(tmp_path: Path) -> None:
    res = await _run(tmp_path, {"pending": [{"cond": "p"}], "gaps": [{"g": 1}], "warnings": ["w"]})
    assert res.pending_signoffs == [{"cond": "p"}]
    assert res.gaps == [{"g": 1}]
    assert res.warnings == [{"kind": "warning", "detail": "w"}]


@pytest.mark.asyncio
async def test_run_dev_agent_accepts_structured_warnings(tmp_path: Path) -> None:
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": [{"kind": "planner", "message": "占位"}]})
    assert res.warnings == [{"kind": "planner", "message": "占位"}]


@pytest.mark.asyncio
async def test_run_dev_agent_tool_order(tmp_path: Path) -> None:
    trace: list[str] = []
    await _run(tmp_path, {"pending": [], "gaps": [], "warnings": []}, trace=trace)
    assert trace == ["read_bundle", "propose_test", "plan"]


@pytest.mark.asyncio
async def test_run_dev_agent_draft_status_and_passed_to_plan(tmp_path: Path) -> None:
    raw = _raw({"kind": "output_voltage", "side": "output", "text": "输出电压 54V", "value": {"typ": 54, "unit": "V"}})
    res = await _run(
        tmp_path,
        {"pending": [], "gaps": [], "warnings": []},
        llm=StubLLM(json_responses=[raw]),
    )
    assert res.bundle_draft["status"] == "draft"
    assert res.bundle_draft["requirements"][0]["conditions"][0]["status"] == "draft"


@pytest.mark.asyncio
async def test_run_dev_agent_rag_degraded_not_in_warnings(tmp_path: Path) -> None:
    """RAG 降级不影响三类分离（warnings 只装 planner 警告）。"""
    gw = StubGateway(error=RuntimeError("gateway down"))
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": ["planner-w"]}, gateway=gw)
    assert res.warnings == [{"kind": "warning", "detail": "planner-w"}]
    assert gw.calls  # 确实尝试过检索


@pytest.mark.asyncio
async def test_run_dev_agent_rag_ok(tmp_path: Path) -> None:
    gw = StubGateway(payload={"items": [{"code": "REQ-9"}]})
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": []}, gateway=gw)
    assert gw.calls[0][0] == "search_requirements"
    assert res.llm_interpretation  # 正常解读


@pytest.mark.asyncio
async def test_run_dev_agent_interpretation_fallback(tmp_path: Path) -> None:
    llm = StubLLM(complete_error=RuntimeError("llm down"))
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": []}, llm=llm)
    assert "[解读不可用]" in res.llm_interpretation


@pytest.mark.asyncio
async def test_run_dev_agent_interpretation_normal(tmp_path: Path) -> None:
    llm = StubLLM(complete_text="综合建议：先签字再排产。")
    res = await _run(tmp_path, {"pending": [], "gaps": [], "warnings": []}, llm=llm)
    assert res.llm_interpretation == "综合建议：先签字再排产。"
    assert llm.complete_calls  # 解读确实调了 LLM


@pytest.mark.asyncio
async def test_run_dev_agent_missing_bundle_raises(tmp_path: Path) -> None:
    llm = StubLLM()
    with pytest.raises(FileNotFoundError):
        await run_dev_agent("NOPE", "需求", llm, StubGateway(), StubAteStudio({}), exports_dir=tmp_path)


# ------------------------------------------------------------- Pydantic AI ----
def test_build_dev_agent_requires_configured_llm(make_settings) -> None:
    with pytest.raises(LLMNotConfiguredError):
        build_dev_agent(LLMClient(make_settings()))


def test_build_dev_agent_constructs(make_settings) -> None:
    llm = LLMClient(make_settings(llm_base="http://127.0.0.1:1/v1", llm_api_key="k", llm_model="qwen3.7-flash"))
    agent = build_dev_agent(llm)
    assert type(agent).__name__ == "Agent"
