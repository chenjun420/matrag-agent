"""LLM 提案 bundle draft（B.3.3）—— draft 红线 + 事实护栏 + kind 闸门。

与 MATRag ``extract/narrative.py`` 同源纪律（spec §5 B.3 / D16）：

- 提案一律 ``status="draft"``：生产侧绝不代签，draft 未经人审不得作为产测判据；
- 事实护栏（fact guard）：``value`` 的每个数值必须能在原文块逐字找到，否则**弃该
  数值**并落 ``review``（留痕，不静默改写）；
- kind/side 必须是本地词表内的 ``(kind, side)``（与 ATEStudio
  ``config/test_method_bindings.yaml`` 对齐），词表外的条目直接弃用；
- measure 类条件若弃值后无判据，**整条弃用**（不得无判据测量）；
- LLM 输出非法时带更强的 ``schema_hint`` 重试一次，仍失败则返回带 ``fallback``
  标记的最小 draft（留痕，不静默吞错）。

本模块不写库、不代签；仅产出结构化 draft 供 :mod:`matrag_agent.agents.dev_agent`
解读与人工评审。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "KNOWN_KIND_SIDES",
    "KIND_UNITS",
    "MEASURE_KINDS",
    "propose_test",
]

#: (kind, side) → 量纲约定；数据来源 ATEStudio ``test_method_bindings.yaml`` v1（25 条）。
#: 与 MATRag ``extract/kinds.py:KIND_UNITS`` 逐字一致（跨仓冻结词表，改动需双方同步）。
KIND_UNITS: dict[tuple[str, str], str] = {
    # ── 输入侧：设置外部激励 ──────────────────────────────────────────────
    ("input_voltage", "input"): "Vac",
    ("input_frequency", "input"): "Hz",
    ("input_type", "input"): "",
    ("load", "input"): "A",
    ("duty", "input"): "%",
    ("temperature", "input"): "degC",
    ("measurement_setup", "input"): "",
    ("command", "input"): "",
    ("test_mode", "input"): "",
    ("power_event", "input"): "",
    ("power_factor", "input"): "",
    ("fault_stimulus", "input"): "",
    ("output_current", "input"): "A",
    # ── 输出侧：读取并判定 ────────────────────────────────────────────────
    ("cap_load", "output"): "uF",
    ("output_voltage", "output"): "V",
    ("output_current", "output"): "A",
    ("output_power", "output"): "W",
    ("ripple", "output"): "mVpp",
    ("efficiency", "output"): "%",
    ("timing", "output"): "ms",
    ("signal_state", "output"): "",
    ("telemetry_value", "output"): "",
    ("protection_action", "output"): "",
    ("presence", "output"): "",
    ("measurement_setup", "output"): "",
}

#: 冻结的 (kind, side) 集合（25 条）；词表外条目一律弃用。
KNOWN_KIND_SIDES: frozenset[tuple[str, str]] = frozenset(KIND_UNITS)

#: 需要数值判据的 kind（弃值后若无判据则整条弃用）。
MEASURE_KINDS: frozenset[str] = frozenset(
    {"output_voltage", "output_current", "output_power", "ripple", "efficiency", "timing"}
)

#: 提案系统提示词（铁律与 MATRag narrative 一致）。
_SYSTEM = (
    "你是电源规格书产测 bundle 提案助手。输入给出原文（MATRag 导出 bundle 的 JSON）"
    "与 kind 词表。任务：只从原文抽取可用于产测评审的条件条目，输出严格 JSON。"
    "铁律：①只能使用原文中逐字出现的数值，不得换算、推断或补全；"
    "②status 一律 draft，绝不代签；③kind/side 必须取自给定词表；"
    "④measure 类条件必须带 value 数值，否则不要输出该条；"
    "⑤text 用原文措辞，不得改写加入新信息；⑥把握不足就不要输出该条。"
)

#: 首次请求的 JSON schema 提示。
_SCHEMA_HINT = (
    '{"product_code":"<产品编码>","requirements":[{"code":"REQ-...","text":"<原文要求陈述>",'
    '"conditions":[{"kind":"<词表内 kind>","side":"input|output","text":"<原文措辞>",'
    '"value":{"min":0,"typ":0,"max":0,"unit":"<单位>"},"method_ref":""}]}]}'
)

#: 重试时更强的 schema 提示（明确必填字段与禁用项）。
_RETRY_HINT = (
    "上一次输出不合法。只输出如下 JSON 对象，不得包含解释或 Markdown 围栏：\n"
    '{"product_code":"<产品编码>","requirements":[{"code":"REQ-01","text":"<原文陈述>",'
    '"conditions":[{"kind":"<词表内 kind>","side":"input|output","text":"<原文措辞>",'
    '"value":{"typ":0,"unit":"<单位>"},"method_ref":""}]}]}\n'
    "requirements 必须是数组；每条 condition 的 kind 必须在词表内，数值必须逐字见于原文。"
)


def _rev(kind: str, heading: str, detail: str, hint: str) -> dict[str, str]:
    """构造一条 review 记录（弃用 / 失败留痕）。"""
    return {"kind": kind, "heading": heading[:255], "detail": detail[:1000], "hint": hint[:1000]}


def _seen_number(num: float, text: str) -> bool:
    """数值是否按字面出现在原文（fact guard；接受 220 / 220.0 / +220 等写法）。"""
    forms = {f"{num:g}", str(num)}
    if num > 0:
        forms.add(f"+{num:g}")
    return any(form in text for form in forms)


def _guard_value(
    raw_value: Any,
    text: str,
    stats: dict[str, int],
    review: list[dict[str, str]],
    heading: str,
) -> dict[str, Any] | None:
    """校验 ``value``：逐个数值走 fact guard；返回保留后的 value（可能为 None）。"""
    if not isinstance(raw_value, dict):
        return None
    kept: dict[str, Any] = {}
    for key in ("min", "typ", "max"):
        num = raw_value.get(key)
        if isinstance(num, bool) or not isinstance(num, (int, float)):
            continue
        if _seen_number(float(num), text):
            kept[key] = round(float(num), 6)
        else:
            stats["values_dropped"] += 1
            review.append(
                _rev(
                    "propose_value_dropped",
                    heading or key,
                    f"提案数值 {num} 未在原文块中出现，已弃用。",
                    "数值须逐字见于原文，不得换算或推断；如确为该行判据，请人工补录。",
                )
            )
    unit = str(raw_value.get("unit") or "").strip()
    if kept and unit and len(unit) <= 32:
        kept["unit"] = unit
    return kept or None


def _iter_conditions(req: dict[str, Any]) -> list[Any]:
    """从一条 requirement 取条件列表（兼容 ``conditions`` 与 input/output 分列）。"""
    conds: list[Any] = []
    if isinstance(req.get("conditions"), list):
        conds.extend(req["conditions"])
    for key in ("input_conditions", "output_conditions"):
        if isinstance(req.get(key), list):
            conds.extend(req[key])
    return conds


def _normalize_draft(
    raw: dict[str, Any], bundle_text: str, product_code: str | None
) -> dict[str, Any]:
    """将 LLM 原始输出归一为 draft bundle（应用 fact guard + kind 闸门）。"""
    stats = {"conditions_draft": 0, "conditions_dropped": 0, "kinds_dropped": 0, "values_dropped": 0}
    review: list[dict[str, str]] = []
    code_prefix = product_code or str(raw.get("product_code") or "UNKNOWN").strip() or "UNKNOWN"
    src_reqs = raw.get("requirements")
    src_reqs = src_reqs if isinstance(src_reqs, list) else []

    reqs_out: list[dict[str, Any]] = []
    for idx, req in enumerate(src_reqs):
        if not isinstance(req, dict):
            continue
        req_code = str(req.get("code") or f"DEV-{code_prefix}-{idx + 1:03d}").strip()
        conds_out: list[dict[str, Any]] = []
        for cond in _iter_conditions(req):
            if not isinstance(cond, dict):
                continue
            kind = str(cond.get("kind") or "").strip()
            side = str(cond.get("side") or "").strip()
            ctext = str(cond.get("text") or "").strip()
            if (kind, side) not in KNOWN_KIND_SIDES:
                stats["kinds_dropped"] += 1
                review.append(
                    _rev(
                        "propose_kind_invalid",
                        f"{kind}/{side}",
                        f"提案 kind/side 不在词表，已弃：{kind}/{side}",
                        "如该概念确需产测，先在提取侧扩展词表与绑定表。",
                    )
                )
                continue
            value = _guard_value(cond.get("value"), bundle_text, stats, review, ctext)
            if kind in MEASURE_KINDS and value is None:
                stats["conditions_dropped"] += 1
                review.append(
                    _rev(
                        "propose_measure_no_value",
                        ctext,
                        "measure 类提案无原文可核数值，已弃（不得无判据测量）。",
                        "如需纳入，请人工补充判据数值并复核。",
                    )
                )
                continue
            conds_out.append(
                {
                    "kind": kind,
                    "side": side,
                    "text": ctext,
                    "value": value,
                    "method_ref": str(cond.get("method_ref") or "").strip(),
                    "status": "draft",
                }
            )
        reqs_out.append(
            {"code": req_code, "text": str(req.get("text") or "").strip(), "conditions": conds_out}
        )

    stats["conditions_draft"] = sum(len(r["conditions"]) for r in reqs_out)
    return {
        "product_code": code_prefix,
        "status": "draft",
        "requirements": reqs_out,
        "review": review,
        "stats": stats,
        "fallback": False,
    }


def _fallback_draft(product_code: str | None, error: str) -> dict[str, Any]:
    """LLM 两次均失败时的最小 draft（留痕，不静默吞错）。"""
    return {
        "product_code": (product_code or "UNKNOWN"),
        "status": "draft",
        "requirements": [],
        "review": [
            _rev(
                "propose_llm_error",
                "LLM 提案失败",
                f"LLM 提案两次均失败：{error}",
                "已返回最小 draft；请检查 LLM 端点（MATRAG_AGENT_LLM_*）后重试或人工录入。",
            )
        ],
        "stats": {"conditions_draft": 0, "conditions_dropped": 0, "kinds_dropped": 0, "values_dropped": 0},
        "fallback": True,
    }


def _build_user(bundle_text: str, product_code: str | None) -> str:
    """构造 LLM 用户提示：原文（截断）+ kind 词表。"""
    catalog = "\n".join(
        f"- {k}/{s} (unit={KIND_UNITS[(k, s)] or '-'})" for (k, s) in sorted(KNOWN_KIND_SIDES)
    )
    code_line = f"产品编码：{product_code}\n" if product_code else ""
    return (
        f"{code_line}kind 词表（kind/side）：\n{catalog}\n\n"
        f"原文（MATRag 导出 bundle JSON，已截断）：\n{bundle_text[:12000]}\n\n"
        "请输出提案 JSON。"
    )


def _looks_like_bundle(raw: Any) -> bool:
    """JSON 结构是否像 bundle（顶层对象且含 requirements 数组）。"""
    return isinstance(raw, dict) and isinstance(raw.get("requirements"), list)


async def propose_test(llm: Any, bundle_text: str, *, product_code: str | None = None) -> dict[str, Any]:
    """LLM 提案 bundle draft（status=draft；数值必见原文；kind 词表闸门）。

    Args:
        llm: LLM 门面（须提供 ``complete_json(system, user, *, schema_hint=...)``）。
        bundle_text: 原文文本（MATRag 导出 bundle 的 JSON 序列化）。
        product_code: 产品编码（用于兜底 requirement code 前缀与回填）。

    Returns:
        归一后的 draft bundle（含 ``status="draft"`` / ``review`` / ``stats``）；
        LLM 两次失败时返回带 ``fallback=True`` 的最小 draft。
    """
    last_error = ""
    for attempt in (0, 1):
        hint = _SCHEMA_HINT if attempt == 0 else _RETRY_HINT
        try:
            raw = llm.complete_json(_SYSTEM, _build_user(bundle_text, product_code), schema_hint=hint)
        except Exception as exc:  # LLM / 网络 / 解析失败：重试一次，仍失败则 fallback
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        if _looks_like_bundle(raw):
            return _normalize_draft(raw, bundle_text, product_code)
        last_error = "LLM 输出缺少 requirements 数组"
    return _fallback_draft(product_code, last_error)
