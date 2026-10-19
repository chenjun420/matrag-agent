"""dev_scripts_agent（Phase B.5）—— 统一接管 ATEStudio 脚本生成职责（D9 替代项）。

输入 ``product_code``（+ 可选 ``requirement_code`` + 自然语言补充要求），从 MATRag 侧
**只读**数据源（``data/exports/{code}.bundle.json`` + ``power_test_methods.yaml`` +
ATEStudio ``test_method_bindings.yaml``）取上下文，LLM 综合生成 **ATEStudio DSL v3.2
脚本（Python）**，与 ``ate_platform.executor`` 兼容（可直接导入 ATEStudio 执行）。
**不**输出 pytest 脚本。

确定性编排（便离线测试与审计）：
``read_bundle`` → ``get_method_spec``（逐条件，带 method_ref 时）→
``get_binding_spec``（逐条件）→ LLM 合成脚本 → 归一（DSL 标记 + 语法校验）。
工具调用前一律经 :func:`check_tool` 白名单闸门。

红线：**不调用 ATEStudio HTTP API**（脚本生成完全在 MATRag 端）；不写库、不代签；
工具失败 / LLM 失败一律**留痕**（``DevScriptsAgentResult.degradations``），绝不静默吞错。
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from matrag_agent.tools.binding_spec import get_binding_spec
from matrag_agent.tools.method_spec import get_method_spec
from matrag_agent.tools.read_bundle import read_bundle

logger = logging.getLogger(__name__)

#: dev_scripts_agent 使用的 LLM 模型（与 matrag-agent LLM 门面默认一致）。
DEV_SCRIPTS_AGENT_MODEL = "qwen3.7-flash"

#: 产出脚本格式标识（ATEStudio DSL v3.2）。
SCRIPT_FORMAT = "ate_dsl_v3.2"

#: DSL 版本标记行（脚本头部必含，供 ATEStudio 导入侧识别）。
DSL_VERSION_MARKER = "# ATE-DSL-VERSION: 3.2"

#: 允许 dev_scripts_agent 调用的工具白名单（Tool name → 用途）。
ALLOWED_TOOLS = frozenset({"read_bundle", "get_method_spec", "get_binding_spec"})

#: 被显式拒绝的工具（工具名 → 理由）；与白名单互斥，命中即 fail-fast。
#: 红线：不写库、不代签、不直调 ATEStudio（脚本由本 agent 端侧生成，ATEStudio 后续导入）。
DENIED_TOOLS: dict[str, str] = {
    "import_bundle": "写 ATEStudio（bundle 提交由人工，Agent 不直调）",
    "import_script": "写 ATEStudio（脚本导入由人工，Agent 端侧生成后交付）",
    "approve_condition": "代签红线：条件签字必须人工（POST /knowledge/conditions/approve）",
    "ingest_document": "写 MATRag 型号 KB",
    "build_domain_kb": "写 MATRag 共享 KB",
    "add_knowledge": "写公共知识（Phase C.1 经人工闸门）",
}

#: dev_scripts_agent 系统提示词（真实部署构造 Pydantic AI Agent 时使用）。
SYSTEM_PROMPT = (
    "你是电源产测脚本生成助手（dev_scripts_agent）。工作流：读取 MATRag 导出的 bundle → "
    "取方法规格（power_test_methods.yaml 四字段）与仪器动作绑定（test_method_bindings.yaml）"
    "→ 综合生成 ATEStudio DSL v3.2 脚本（Python，调用 ate_platform.executor 兼容 API）。"
    "铁律：只调用白名单工具；不调用 ATEStudio HTTP API；不输出 pytest；destructive 步骤须"
    "标注并给出恢复（power_cycle）建议；数值必须能追溯到 bundle 原文。"
)

#: 生成脚本的 LLM 系统提示词（合成阶段）。
_LLM_SYSTEM = (
    "你是 ATEStudio 产测脚本生成器。基于给定的结构化上下文（bundle 条件 + 方法规格 + 仪器"
    "动作绑定），生成一段 **ATEStudio DSL v3.2 Python 脚本**：脚本须可直接被 ate_platform."
    "executor 执行；用 from ate_platform.executor.context_proxy import ContextProxy, measure "
    "并使用 @measure 装饰器声明输出。只输出 Python 代码，不要 Markdown 围栏，不要解释。"
    "destructive 步骤前必须加恢复/安全提示。不得编造原文中未出现的数值。"
)

#: LLM 生成不可用时的确定性脚本骨架（可被 ``ast.parse``，留痕降级）。
_FALLBACK_SKELETON = (
    '"""ATEStudio DSL v3.2 脚本骨架（LLM 生成不可用时的确定性回退，需人工补全）。"""\n'
    "from ate_platform.executor.context_proxy import ContextProxy, measure\n"
    "\n"
    "\n"
    '@measure("result")\n'
    "def test_placeholder(proxy: ContextProxy) -> None:\n"
    '    """占位步骤：请按 bundle 条件与绑定补全实际激励/测量。"""\n'
    '    proxy["result"] = None\n'
)


class ToolNotAllowedError(PermissionError):
    """工具不在 dev_scripts_agent 白名单（或命中黑名单）—— 拒绝执行。"""


@dataclass
class DevScriptsAgentResult:
    """dev_scripts_agent 运行结果（DSL v3.2 脚本 + 依赖 + 风险 + 降级留痕）。

    Attributes:
        product_code: 产品编码。
        requirement_code: 目标需求编号（可选；缺省则覆盖 bundle 内全部需求）。
        script_content: 生成的 ATEStudio DSL v3.2 脚本内容（Python，可 ``ast.parse``）。
        script_format: 脚本格式标识（默认 ``ate_dsl_v3.2``）。
        dependencies: 依赖的仪器资源名列表（源自绑定表 ``resource``）。
        risks: 风险标注（destructive 步骤 / 无绑定条件等）。
        degradations: 降级留痕（工具 / LLM 失败记录；空表示全链路正常）。
    """

    product_code: str
    requirement_code: str | None
    script_content: str
    script_format: str = SCRIPT_FORMAT
    dependencies: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    degradations: list[str] = field(default_factory=list)


def check_tool(name: str) -> None:
    """校验工具名；不在白名单或命中黑名单则抛 :class:`ToolNotAllowedError`。

    沿用 ``clients/gateway.py`` 双轨模式（白名单 + 显式黑名单）。
    """
    if name in DENIED_TOOLS:
        raise ToolNotAllowedError(f"工具 {name!r} 被显式拒绝：{DENIED_TOOLS[name]}")
    if name not in ALLOWED_TOOLS:
        raise ToolNotAllowedError(
            f"工具 {name!r} 不在 dev_scripts_agent 白名单；允许: {sorted(ALLOWED_TOOLS)}"
        )


def _record(trace: list[str] | None, name: str) -> None:
    """记录一次工具调用（顺序可观测，供审计 / 测试）。"""
    if trace is not None:
        trace.append(name)


def _select_requirements(
    bundle: dict[str, Any], requirement_code: str | None
) -> tuple[list[dict[str, Any]], bool]:
    """从 bundle 选出目标需求。

    Returns:
        ``(requirements, missing)``：``requirement_code`` 给定但未命中时 ``missing=True``。
    """
    raw = bundle.get("requirements")
    reqs = [req for req in raw if isinstance(req, dict)] if isinstance(raw, list) else []
    key = str(requirement_code or "").strip()
    if not key:
        return reqs, False
    matched = [
        req
        for req in reqs
        if key
        in {
            str(req.get("requirement_code") or "").strip(),
            str(req.get("spec_requirement_id") or "").strip(),
        }
    ]
    if matched:
        return matched, False
    return [], True


def _iter_conditions(req: dict[str, Any]) -> list[dict[str, Any]]:
    """从一条 requirement 展开条件（兼容 input/output 分列；``side`` 由 ``role`` 校正）。"""
    out: list[dict[str, Any]] = []
    for key, default_side in (("input_conditions", "input"), ("output_conditions", "output")):
        items = req.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            side = role if role in ("input", "output") else default_side
            out.append(
                {
                    "kind": str(item.get("kind") or "").strip(),
                    "side": side,
                    "text": str(item.get("text") or "").strip(),
                    "method_ref": str(item.get("method_ref") or "").strip(),
                    "value": item.get("value"),
                }
            )
    return out


def _extract_code(raw: str) -> str:
    """剥离 Markdown 围栏，取出纯代码；无围栏则原样返回。"""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _header(product_code: str) -> str:
    """生成脚本规范头（含 DSL v3.2 标记）。"""
    return (
        f"{DSL_VERSION_MARKER}\n"
        f"# script-format: {SCRIPT_FORMAT}\n"
        f"# product: {product_code}\n"
    )


def _finalize_script(product_code: str, raw_code: str, degradations: list[str]) -> str:
    """归一脚本：剥围栏 → 补 DSL 头 → ``ast.parse`` 校验；不合法则回退确定性骨架。"""
    body = _extract_code(raw_code)
    if not body:
        degradations.append("LLM 未产出脚本内容，已回退确定性骨架")
        body = _FALLBACK_SKELETON

    script = body if DSL_VERSION_MARKER in body else _header(product_code) + body

    try:
        ast.parse(script)
    except SyntaxError as exc:
        degradations.append(
            f"LLM 脚本语法错误（{exc.msg} @ line {exc.lineno}），已回退确定性骨架"
        )
        script = _header(product_code) + _FALLBACK_SKELETON
    return script


def _build_user_prompt(
    product_code: str,
    requirements: list[dict[str, Any]],
    conditions: list[dict[str, Any]],
    method_specs: list[dict[str, Any]],
    binding_specs: list[dict[str, Any]],
    extra_requirements: str | None,
) -> str:
    """构造合成阶段的用户提示（结构化上下文 JSON，截断）。"""
    payload = {
        "product_code": product_code,
        "requirements": [
            {"requirement_code": req.get("requirement_code"), "title": req.get("title")}
            for req in requirements
        ],
        "conditions": conditions,
        "method_specs": method_specs,
        "binding_specs": binding_specs,
        "extra_requirements": extra_requirements or "",
    }
    return json.dumps(payload, ensure_ascii=False)[:12000] + "\n\n请生成 ATEStudio DSL v3.2 脚本（Python）。"


async def run_dev_scripts_agent(
    product_code: str,
    llm: Any,
    *,
    requirement_code: str | None = None,
    extra_requirements: str | None = None,
    exports_dir: Any = None,
    methods_path: Any = None,
    bindings_path: Any = None,
    tool_overrides: dict[str, Any] | None = None,
    trace: list[str] | None = None,
) -> DevScriptsAgentResult:
    """dev_scripts_agent 入口：bundle + 方法规格 + 绑定 → LLM 合成 DSL v3.2 脚本。

    Args:
        product_code: 产品编码（决定读取哪个 bundle）。
        llm: LLM 门面（``complete``）。
        requirement_code: 目标需求编号（可选；缺省覆盖 bundle 内全部需求）。
        extra_requirements: 自然语言补充要求（可选，参与合成）。
        exports_dir: bundle 导出目录覆盖（测试注入 ``tmp_path``）。
        methods_path: 方法库路径覆盖（测试注入）。
        bindings_path: 绑定表路径覆盖（测试注入）。
        tool_overrides: 工具实现覆盖（测试 stub；键为工具名）。
        trace: 可选调用记录列表；按顺序追加工具名。

    Returns:
        :class:`DevScriptsAgentResult`；``script_content`` 恒为可 ``ast.parse`` 的 DSL v3.2 脚本；
        工具 / LLM / requirement 缺失的降级记录在 ``degradations``。

    Raises:
        FileNotFoundError: bundle 文件不存在（fail-fast）。
        ToolNotAllowedError: 试图调用白名单外工具。
    """
    degradations: list[str] = []
    overrides = tool_overrides or {}

    # 1) bundle（缺失 fail-fast，与 dev_agent 一致）
    check_tool("read_bundle")
    _record(trace, "read_bundle")
    read_impl = overrides.get("read_bundle", read_bundle)
    bundle: dict[str, Any] = read_impl(product_code, exports_dir=exports_dir)

    # 2) 选目标需求
    requirements, missing = _select_requirements(bundle, requirement_code)
    if missing:
        degradations.append(
            f"requirement_code={requirement_code!r} 未在 bundle 中找到，已忽略（无目标需求）"
        )

    # 3) 逐条件取方法规格 / 绑定
    method_impl = overrides.get("get_method_spec", get_method_spec)
    binding_impl = overrides.get("get_binding_spec", get_binding_spec)
    conditions: list[dict[str, Any]] = []
    method_specs: list[dict[str, Any]] = []
    binding_specs: list[dict[str, Any]] = []
    dependencies: set[str] = set()
    risks: list[str] = []

    for req in requirements:
        for cond in _iter_conditions(req):
            conditions.append(cond)
            kind = cond["kind"]
            side = cond["side"]
            method_ref = cond["method_ref"]

            if method_ref:
                check_tool("get_method_spec")
                _record(trace, "get_method_spec")
                try:
                    spec = method_impl(method_ref, methods_path=methods_path)
                except Exception as exc:  # 工具失败留痕，不阻断脚本生成
                    degradations.append(
                        f"get_method_spec({method_ref!r}) 失败：{type(exc).__name__}: {exc}"
                    )
                    spec = None
                if spec is not None:
                    method_specs.append(spec)
                    if not spec.get("found"):
                        degradations.append(
                            f"方法 {method_ref!r} 未在方法库中找到，已回退空规格"
                        )

            if not kind:
                continue
            check_tool("get_binding_spec")
            _record(trace, "get_binding_spec")
            try:
                binding = binding_impl(kind, side, bindings_path=bindings_path)
            except Exception as exc:  # 工具失败留痕，不阻断脚本生成
                degradations.append(
                    f"get_binding_spec({kind}/{side}) 失败：{type(exc).__name__}: {exc}"
                )
                continue
            binding_specs.append(binding)
            if binding.get("found"):
                dependencies.update(str(res) for res in binding.get("resource") or [])
                if binding.get("destructive"):
                    risks.append(
                        f"destructive 步骤 {kind}/{side}（action={binding.get('action')}）"
                        "可能损坏被测件：需前后加恢复步骤，必要时 power_cycle 后复测"
                    )
            else:
                risks.append(
                    f"条件 {kind}/{side} 无绑定（action 未知）：无法排步骤，"
                    "需人工补 test_method_bindings.yaml"
                )

    # 4) LLM 合成脚本 → 归一
    prompt = _build_user_prompt(
        product_code, requirements, conditions, method_specs, binding_specs, extra_requirements
    )
    try:
        raw = llm.complete(_LLM_SYSTEM, prompt)
    except Exception as exc:  # LLM / 网络失败 → 回退（留痕，不静默）
        logger.warning("LLM 脚本生成失败：%s", exc)
        degradations.append(f"LLM 脚本生成失败：{type(exc).__name__}: {exc}")
        raw = ""

    script = _finalize_script(product_code, str(raw or ""), degradations)

    return DevScriptsAgentResult(
        product_code=product_code,
        requirement_code=requirement_code,
        script_content=script,
        script_format=SCRIPT_FORMAT,
        dependencies=sorted(dependencies),
        risks=risks,
        degradations=degradations,
    )


def build_dev_scripts_agent(
    llm: Any,
    *,
    exports_dir: Any = None,
    methods_path: Any = None,
    bindings_path: Any = None,
) -> Any:
    """构造真实部署用的 Pydantic AI Agent（model=``DEV_SCRIPTS_AGENT_MODEL``）+ 白名单工具。

    Agent 循环仅在真实 LLM 端点可用时使用；离线测试走 :func:`run_dev_scripts_agent`
    的确定性编排。模型对象来自 LLM 门面（``llm.build_model()``），构造不触网。

    Raises:
        LLMNotConfiguredError: LLM 未配置（fail-closed）。
    """
    from pydantic_ai import Agent

    model = llm.build_model()

    def tool_read_bundle(product_code: str) -> dict[str, Any]:
        """读取 MATRag 导出的 bundle。"""
        return read_bundle(product_code, exports_dir=exports_dir)

    def tool_get_method_spec(method_id: str) -> dict[str, Any]:
        """取方法四字段规格（power_test_methods.yaml）。"""
        return get_method_spec(method_id, methods_path=methods_path)

    def tool_get_binding_spec(kind: str, side: str) -> dict[str, Any]:
        """取 (kind, side) 的仪器动作绑定。"""
        return get_binding_spec(kind, side, bindings_path=bindings_path)

    return Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        tools=[tool_read_bundle, tool_get_method_spec, tool_get_binding_spec],
    )
