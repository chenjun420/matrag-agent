"""dev_scripts_agent（Phase B.5）单元测试：全离线（stub LLM / 工具 / 临时数据源）。

覆盖：端到端 stub LLM + stub 工具、脚本结构（DSL v3.2 markers）、``dependencies``、
``risks``（destructive 警告）、方法未命中 fallback、绑定未命中 fallback、脚本 ``ast.parse``、
``degradations`` 留痕（工具 / LLM / requirement 缺失）、白名单双轨 + CI pin、
工具级 ``get_method_spec`` / ``get_binding_spec`` 归一，以及 Pydantic AI Agent 构造。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from matrag_agent.agents import DevScriptsAgentResult, run_dev_scripts_agent
from matrag_agent.agents.dev_scripts_agent import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    DSL_VERSION_MARKER,
    SCRIPT_FORMAT,
    ToolNotAllowedError,
    build_dev_scripts_agent,
    check_tool,
)
from matrag_agent.clients.llm import LLMClient, LLMNotConfiguredError
from matrag_agent.tools.binding_spec import (
    FALLBACK_BINDINGS,
    BindingSpecError,
    get_binding_spec,
)
from matrag_agent.tools.method_spec import MethodSpecError, get_method_spec

# ------------------------------------------------------------------ stubs ----
GENERATED_SCRIPT = """\
from ate_platform.executor.context_proxy import ContextProxy, measure


@measure("vout")
def test_output_voltage(proxy: ContextProxy) -> None:
    dmm = proxy.resource("DMM_1")
    proxy["vout"] = 0.0
"""


class StubLLM:
    """离线 LLM stub：``complete`` 回放固定脚本；可注入异常。"""

    def __init__(self, *, text: str = GENERATED_SCRIPT, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(
        self, system: str, user: str, *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        self.calls.append({"system": system, "user": user})
        if self.error is not None:
            raise self.error
        return self.text


# ------------------------------------------------------------------ 数据 ----
BUNDLE: dict[str, Any] = {
    "bundle_version": "1.0",
    "product_code": "PA601-D54A",
    "requirements": [
        {
            "requirement_code": "SR-PA601-D54A-1200",
            "spec_requirement_id": "SR-PA601-D54A-1200",
            "title": "额定输出电压",
            "input_conditions": [
                {
                    "kind": "input_voltage",
                    "text": "额定输入 220Vac",
                    "role": "input",
                    "value": {"typ": 220.0, "unit": "Vac"},
                    "method_ref": "M-INPUT-SLOW",
                }
            ],
            "output_conditions": [
                {
                    "kind": "output_voltage",
                    "text": "额定输出电压 -54V",
                    "role": "output",
                    "value": {"min": -54.0, "unit": "V"},
                    "method_ref": "M-REGULATION",
                }
            ],
        },
        {
            "requirement_code": "SR-PA601-D54A-1300",
            "spec_requirement_id": "SR-PA601-D54A-1300",
            "title": "短路保护",
            "input_conditions": [
                {
                    "kind": "fault_stimulus",
                    "text": "输出短路",
                    "role": "input",
                    "value": None,
                    "method_ref": "",
                }
            ],
            "output_conditions": [],
        },
    ],
}

_METHODS_YAML = """\
- method_id: M-INPUT-SLOW
  zh: 输入缓慢变动测试
  en: Input Slow Variation Test
  category: input
  parameters:
    - 输入变化速率：3Vac/min
  instruments:
    - 可编程交流源
  interfaces:
    - 输入端子接可编程交流源
  metrics:
    - 关断点/启动点输入电压
- method_id: M-REGULATION
  zh: 调整率测试
  en: Load and Line Regulation Test
  category: output
  parameters:
    - 负载：10%->100%
  instruments:
    - 数字万用表
  interfaces:
    - 输出端子接电子负载
  metrics:
    - 负载调整率
"""

_BINDINGS_YAML = """\
version: 1
bindings:
  - kind: input_voltage
    side: input
    resource: [PSU_1]
    action: set_ac_input
    settle_s: 2.0
    destructive: false
    unit: Vac
  - kind: output_voltage
    side: output
    resource: [DMM_1]
    action: measure_dc_voltage
    settle_s: 0.3
    destructive: false
    unit: V
  - kind: fault_stimulus
    side: input
    resource: [RELAY_MATRIX]
    action: inject_fault
    settle_s: 1.0
    destructive: true
    unit: ""
"""


def _write_bundle(dir_path: Path, code: str, payload: dict[str, Any]) -> None:
    (dir_path / f"{code}.bundle.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    """写入临时方法库与绑定表，返回 ``(methods_path, bindings_path)``。"""
    methods = tmp_path / "power_test_methods.yaml"
    methods.write_text(_METHODS_YAML, encoding="utf-8")
    bindings = tmp_path / "test_method_bindings.yaml"
    bindings.write_text(_BINDINGS_YAML, encoding="utf-8")
    return methods, bindings


async def _run(
    tmp_path: Path,
    sources: tuple[Path, Path],
    *,
    bundle: dict[str, Any] | None = None,
    llm: StubLLM | None = None,
    tool_overrides: dict[str, Any] | None = None,
    requirement_code: str | None = None,
    trace: list[str] | None = None,
) -> DevScriptsAgentResult:
    """端到端跑 run_dev_scripts_agent（离线）。"""
    _write_bundle(tmp_path, "PA601-D54A", bundle or BUNDLE)
    methods, bindings = sources
    return await run_dev_scripts_agent(
        "PA601-D54A",
        llm if llm is not None else StubLLM(),
        requirement_code=requirement_code,
        exports_dir=tmp_path,
        methods_path=methods,
        bindings_path=bindings,
        tool_overrides=tool_overrides,
        trace=trace,
    )


# ------------------------------------------------------------ 白名单闸门 ----
def test_allowed_tools_pinned() -> None:
    """CI pin：dev_scripts_agent 工具白名单固定。"""
    assert ALLOWED_TOOLS == {"read_bundle", "get_method_spec", "get_binding_spec"}


def test_denied_tools_pinned() -> None:
    """CI pin：拒绝清单固定（写 / 代签 / 跨仓导入）。"""
    assert set(DENIED_TOOLS) == {
        "import_bundle",
        "import_script",
        "approve_condition",
        "ingest_document",
        "build_domain_kb",
        "add_knowledge",
    }


def test_allowed_denied_disjoint() -> None:
    assert ALLOWED_TOOLS.isdisjoint(DENIED_TOOLS)


def test_check_tool_rejects_denied() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("import_script")


def test_check_tool_rejects_unknown() -> None:
    with pytest.raises(ToolNotAllowedError):
        check_tool("definitely_not_a_tool")


def test_check_tool_allows_whitelisted() -> None:
    for name in ALLOWED_TOOLS:
        check_tool(name)  # 不抛


# ------------------------------------------------------ get_method_spec ----
def test_get_method_spec_reads_yaml(sources: tuple[Path, Path]) -> None:
    methods, _ = sources
    spec = get_method_spec("M-INPUT-SLOW", methods_path=methods)
    assert spec["found"] is True
    assert spec["source"] == "yaml"
    assert spec["zh"] == "输入缓慢变动测试"
    assert spec["category"] == "input"
    assert spec["parameters"] == ["输入变化速率：3Vac/min"]
    assert spec["instruments"] == ["可编程交流源"]
    assert spec["interfaces"] == ["输入端子接可编程交流源"]
    assert spec["metrics"] == ["关断点/启动点输入电压"]


def test_get_method_spec_not_found_fallback(sources: tuple[Path, Path]) -> None:
    methods, _ = sources
    spec = get_method_spec("M-NOPE", methods_path=methods)
    assert spec["found"] is False
    assert spec["source"] == "not_found"
    assert spec["parameters"] == []
    assert spec["instruments"] == []


def test_get_method_spec_empty_id(sources: tuple[Path, Path]) -> None:
    methods, _ = sources
    assert get_method_spec("", methods_path=methods)["source"] == "empty_id"


def test_get_method_spec_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(MethodSpecError):
        get_method_spec("M-ANY", methods_path=tmp_path / "nope.yaml")


# ----------------------------------------------------- get_binding_spec ----
def test_get_binding_spec_reads_yaml(sources: tuple[Path, Path]) -> None:
    _, bindings = sources
    spec = get_binding_spec("fault_stimulus", "input", bindings_path=bindings)
    assert spec["found"] is True
    assert spec["source"] == "bindings_table"
    assert spec["action"] == "inject_fault"
    assert spec["destructive"] is True
    assert spec["resource"] == ["RELAY_MATRIX"]
    assert spec["settle_s"] == 1.0


def test_get_binding_spec_not_found_fallback(sources: tuple[Path, Path]) -> None:
    _, bindings = sources
    spec = get_binding_spec("bogus_kind", "input", bindings_path=bindings)
    assert spec["found"] is False
    assert spec["source"] == "unsupported"
    assert spec["action"] == "unsupported"
    assert spec["resource"] == []


def test_get_binding_spec_missing_file_uses_base_set(tmp_path: Path) -> None:
    """绑定表文件不可用 → 回退硬编码基础集（source 留痕 fallback）。"""
    spec = get_binding_spec("output_voltage", "output", bindings_path=tmp_path / "nope.yaml")
    assert spec["found"] is True
    assert spec["source"] == "fallback"
    assert spec["action"] == FALLBACK_BINDINGS[("output_voltage", "output")]["action"]


def test_get_binding_spec_malformed_yaml_raises(tmp_path: Path) -> None:
    """命中文件但结构非法 → fail-fast（不静默降级损坏配置）。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("not-a-mapping\n", encoding="utf-8")
    with pytest.raises(BindingSpecError):
        get_binding_spec("output_voltage", "output", bindings_path=bad)


# --------------------------------------------------- 端到端脚本生成 ----
@pytest.mark.asyncio
async def test_run_dev_scripts_agent_end_to_end(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources)
    assert isinstance(res, DevScriptsAgentResult)
    assert res.product_code == "PA601-D54A"
    assert res.requirement_code is None
    assert res.script_format == "ate_dsl_v3.2"
    assert res.script_content
    assert res.degradations == []


@pytest.mark.asyncio
async def test_script_contains_dsl_markers(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources)
    assert DSL_VERSION_MARKER in res.script_content
    assert SCRIPT_FORMAT in res.script_content
    assert res.script_content.lstrip().startswith(DSL_VERSION_MARKER)


@pytest.mark.asyncio
async def test_script_parses_with_ast(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources)
    ast.parse(res.script_content)  # 不抛即通过


@pytest.mark.asyncio
async def test_dependencies_include_bundle_resources(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources)
    assert res.dependencies == ["DMM_1", "PSU_1", "RELAY_MATRIX"]


@pytest.mark.asyncio
async def test_dependencies_scoped_to_selected_requirement(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources, requirement_code="SR-PA601-D54A-1200")
    assert res.requirement_code == "SR-PA601-D54A-1200"
    assert res.dependencies == ["DMM_1", "PSU_1"]
    assert res.risks == []


@pytest.mark.asyncio
async def test_risks_include_destructive_warning(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources)
    assert any("destructive" in risk and "power_cycle" in risk for risk in res.risks)


@pytest.mark.asyncio
async def test_risks_include_unbound_condition(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    bundle = json.loads(json.dumps(BUNDLE))
    bundle["requirements"][0]["output_conditions"][0]["kind"] = "bogus_kind"
    res = await _run(tmp_path, sources, bundle=bundle)
    assert any("无绑定" in risk for risk in res.risks)


@pytest.mark.asyncio
async def test_method_not_found_degradation(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    bundle = json.loads(json.dumps(BUNDLE))
    bundle["requirements"][0]["output_conditions"][0]["method_ref"] = "M-NOPE"
    res = await _run(tmp_path, sources, bundle=bundle)
    assert any("未在方法库中找到" in item for item in res.degradations)


@pytest.mark.asyncio
async def test_llm_failure_fallback_and_degradation(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    llm = StubLLM(error=RuntimeError("endpoint down"))
    res = await _run(tmp_path, sources, llm=llm)
    assert any("LLM 脚本生成失败" in item for item in res.degradations)
    # 回退骨架仍含 DSL 标记且语法合法
    assert DSL_VERSION_MARKER in res.script_content
    ast.parse(res.script_content)


@pytest.mark.asyncio
async def test_llm_invalid_syntax_fallback(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources, llm=StubLLM(text="def broken(:\n    pass"))
    assert any("语法错误" in item for item in res.degradations)
    ast.parse(res.script_content)


@pytest.mark.asyncio
async def test_tool_failure_degradation(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    """工具 API 找不到 → 留痕（不静默吞错）。"""

    def _boom(_kind: str, _side: str, **_kwargs: Any) -> dict[str, Any]:
        raise BindingSpecError("binding API unavailable")

    res = await _run(tmp_path, sources, tool_overrides={"get_binding_spec": _boom})
    assert any("get_binding_spec" in item for item in res.degradations)
    assert res.dependencies == []
    ast.parse(res.script_content)


@pytest.mark.asyncio
async def test_requirement_not_found_degradation(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    res = await _run(tmp_path, sources, requirement_code="SR-NOPE")
    assert res.requirement_code == "SR-NOPE"
    assert any("未在 bundle 中找到" in item for item in res.degradations)
    ast.parse(res.script_content)


@pytest.mark.asyncio
async def test_tool_order(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    trace: list[str] = []
    await _run(tmp_path, sources, trace=trace)
    assert trace[0] == "read_bundle"
    assert set(trace) <= ALLOWED_TOOLS
    assert "get_method_spec" in trace
    assert "get_binding_spec" in trace


@pytest.mark.asyncio
async def test_missing_bundle_raises(tmp_path: Path, sources: tuple[Path, Path]) -> None:
    methods, bindings = sources
    with pytest.raises(FileNotFoundError):
        await run_dev_scripts_agent(
            "NOPE",
            StubLLM(),
            exports_dir=tmp_path,
            methods_path=methods,
            bindings_path=bindings,
        )


# ------------------------------------------------------------- Pydantic AI ----
def test_build_dev_scripts_agent_requires_configured_llm(make_settings) -> None:
    with pytest.raises(LLMNotConfiguredError):
        build_dev_scripts_agent(LLMClient(make_settings()))


def test_build_dev_scripts_agent_constructs(make_settings) -> None:
    llm = LLMClient(
        make_settings(llm_base="http://127.0.0.1:1/v1", llm_api_key="k", llm_model="qwen3.7-flash")
    )
    agent = build_dev_scripts_agent(llm)
    assert type(agent).__name__ == "Agent"
