"""取 ``(kind, side)`` 的仪器动作绑定（action/settle_s/destructive/...）。

Phase B.5 ``dev_scripts_agent`` 的只读数据源之一：条件种类 → 仪器动作的翻译表。权威表
在 ATEStudio ``config/test_method_bindings.yaml``（MATRag 侧镜像其 ``(kind, side)`` 词表）。

解析优先级：
1. 显式 ``bindings_path``（测试注入）；
2. ``<matrag_root>/data/domain/source/test_method_bindings.yaml``（如主仓将来落副本）；
3. 兄弟仓 ``<matrag_root>/../ATEStudio/config/test_method_bindings.yaml``（权威表）；
4. 均不可用 → 回退 :data:`FALLBACK_BINDINGS`（硬编码基础集，留痕 ``source="fallback"``）。

纪律：命中真实表但 YAML 损坏 / 结构非法 → fail-fast 抛 :class:`BindingSpecError`
（不静默降级损坏配置）；**文件整体不可用**与**单个 kind 未命中**才走 fallback。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from matrag_agent.config import Settings, get_settings

__all__ = [
    "BindingSpecError",
    "FALLBACK_BINDINGS",
    "get_binding_spec",
    "load_bindings",
]

#: 基础绑定集（硬编码）：绑定表整体不可用或单个 ``(kind, side)`` 未命中时的兜底。
#: 元组字段顺序：(kind, side, action, settle_s, destructive, resources, setup_group,
#: requires, unit)。内容镜像 ATEStudio ``test_method_bindings.yaml`` v1（25 条）。
_BASE_BINDINGS: tuple[
    tuple[str, str, str, float, bool, tuple[str, ...], str, tuple[str, ...], str], ...
] = (
    # ── 输入侧: 设置外部激励 ──────────────────────────────────────────────
    ("input_voltage", "input", "set_ac_input", 2.0, False, ("PSU_1",), "ac_input", (), "Vac"),
    ("input_frequency", "input", "set_line_frequency", 1.0, False, ("PSU_1",), "ac_input", ("input_voltage",), "Hz"),
    ("input_type", "input", "select_input_source", 1.0, False, ("RELAY_MATRIX",), "input_route", (), ""),
    ("load", "input", "set_electronic_load", 1.5, False, ("ELOAD_1",), "load_level", (), "A"),
    ("cap_load", "output", "verify_cap_load_capability", 0.5, False, ("CONTROL_IF",), "telemetry", (), "uF"),
    ("duty", "input", "set_load_duty", 1.5, False, ("ELOAD_1",), "load_level", ("load",), "%"),
    ("temperature", "input", "set_chamber_temperature", 1800.0, False, ("CHAMBER_1",), "ambient", (), "degC"),
    ("measurement_setup", "input", "configure_measurement", 0.2, False, ("SCOPE_1",), "measurement", (), ""),
    ("command", "input", "issue_command", 0.5, False, ("PSU_1",), "sequence", (), ""),
    ("test_mode", "input", "set_test_mode", 0.5, False, ("CONTROL_IF",), "sequence", (), ""),
    ("power_event", "input", "trigger_power_event", 1.0, False, ("PSU_1", "RELAY_MATRIX"), "power_transient", (), ""),
    ("power_factor", "input", "configure_pf_measurement", 0.5, False, ("POWER_ANALYZER",), "measurement", (), ""),
    ("fault_stimulus", "input", "inject_fault", 1.0, True, ("RELAY_MATRIX",), "fault_injection", (), ""),
    # ── 输出侧: 读取并判定 ────────────────────────────────────────────────
    ("output_voltage", "output", "measure_dc_voltage", 0.3, False, ("DMM_1",), "measurement", (), "V"),
    ("output_current", "output", "measure_dc_current", 0.3, False, ("DMM_2",), "measurement", (), "A"),
    ("output_current", "input", "measure_input_current", 0.5, False, ("DMM_1",), "measurement", (), "A"),
    ("output_power", "output", "measure_output_power", 0.3, False, ("POWER_ANALYZER",), "measurement", (), "W"),
    ("ripple", "output", "measure_ripple_20mhz", 0.2, False, ("SCOPE_1",), "measurement", (), "mVpp"),
    ("efficiency", "output", "measure_efficiency", 1.0, False, ("POWER_ANALYZER",), "measurement", (), "%"),
    ("timing", "output", "measure_transient_response", 0.2, False, ("SCOPE_1",), "measurement", (), "ms"),
    ("signal_state", "output", "read_signal_state", 0.2, False, ("CONTROL_IF",), "telemetry", (), ""),
    ("telemetry_value", "output", "read_telemetry", 0.5, False, ("CONTROL_IF",), "telemetry", (), ""),
    ("protection_action", "output", "verify_protection_action", 1.0, False, ("CONTROL_IF",), "protection", (), ""),
    ("presence", "output", "verify_capability_present", 0.3, False, ("CONTROL_IF",), "telemetry", (), ""),
    ("measurement_setup", "output", "configure_measurement_point", 0.3, False, ("DMM_1",), "measurement", (), ""),
)

#: 硬编码基础集（``(kind, side)`` → 绑定 dict）。
FALLBACK_BINDINGS: dict[tuple[str, str], dict[str, Any]] = {
    (kind, side): {
        "kind": kind,
        "side": side,
        "action": action,
        "settle_s": settle_s,
        "destructive": destructive,
        "resource": list(resources),
        "setup_group": setup_group,
        "requires": list(requires),
        "unit": unit,
    }
    for (kind, side, action, settle_s, destructive, resources, setup_group, requires, unit) in _BASE_BINDINGS
}

#: 相对 MATRag 根目录的绑定表路径（主仓镜像，可选）。
_MAIN_REPO_RELPATH = Path("data") / "domain" / "source" / "test_method_bindings.yaml"

#: 相对 MATRag 兄弟仓 ATEStudio 的权威绑定表路径。
_ATESTUDIO_RELPATH = Path("ATEStudio") / "config" / "test_method_bindings.yaml"


class BindingSpecError(RuntimeError):
    """绑定表结构非法（fail-fast；不静默降级损坏配置）。"""


def _candidate_paths(bindings_path: Path | None, settings: Settings) -> list[Path]:
    """按优先级给出候选绑定表路径（显式路径优先）。"""
    if bindings_path is not None:
        return [Path(bindings_path)]
    root = Path(settings.matrag_root)
    return [root / _MAIN_REPO_RELPATH, root.parent / _ATESTUDIO_RELPATH]


def _coerce_str_list(raw: Any) -> list[str]:
    """把资源 / 前置动作字段归一为 ``list[str]``。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _normalize(entry: dict[str, Any], *, kind: str, side: str) -> dict[str, Any]:
    """把一条绑定记录归一为固定字段结构。"""
    settle = entry.get("settle_s")
    settle_s = float(settle) if isinstance(settle, (int, float)) and not isinstance(settle, bool) else 0.0
    return {
        "kind": kind,
        "side": side,
        "action": str(entry.get("action") or ""),
        "settle_s": settle_s,
        "destructive": bool(entry.get("destructive", False)),
        "resource": _coerce_str_list(entry.get("resource")),
        "setup_group": str(entry.get("setup_group") or ""),
        "requires": _coerce_str_list(entry.get("requires")),
        "unit": str(entry.get("unit") or ""),
    }


def _index_bindings(data: Any, path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """把绑定表 YAML 解析为 ``(kind, side)`` → 绑定 dict 的索引。

    Raises:
        BindingSpecError: 顶层不是映射 / 缺 ``bindings`` 列表。
    """
    if not isinstance(data, dict):
        raise BindingSpecError(f"绑定表顶层必须是映射: {path}")
    rows = data.get("bindings")
    if not isinstance(rows, list):
        raise BindingSpecError(f"绑定表缺少 bindings 列表: {path}")
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip()
        side = str(row.get("side") or "").strip()
        if kind and side:
            index[(kind, side)] = _normalize(row, kind=kind, side=side)
    return index


def load_bindings(
    *, bindings_path: Path | None = None, settings: Settings | None = None
) -> tuple[dict[tuple[str, str], dict[str, Any]], str]:
    """加载绑定表并返回 ``(索引, 来源)``。

    依次尝试候选路径；命中即解析并返回 ``source="bindings_table"``。全部不可用 → 返回
    硬编码基础集 ``source="fallback"``。

    Raises:
        BindingSpecError: 命中的文件 YAML 损坏或结构非法（fail-fast）。
    """
    resolved_settings = settings or get_settings()
    for path in _candidate_paths(bindings_path, resolved_settings):
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise BindingSpecError(f"绑定表 YAML 解析失败: {path}: {exc}") from exc
        return _index_bindings(data, path), "bindings_table"
    return {pair: dict(entry) for pair, entry in FALLBACK_BINDINGS.items()}, "fallback"


def get_binding_spec(
    kind: str,
    side: str,
    *,
    bindings_path: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """取 ``(kind, side)`` 的仪器动作绑定。

    Args:
        kind: 条件种类（如 ``output_voltage``）。
        side: 侧别（``input`` / ``output``）。
        bindings_path: 显式绑定表路径（测试注入；缺省按 :func:`_candidate_paths` 解析）。
        settings: 显式配置对象（默认 :func:`get_settings`）。

    Returns:
        ``{"kind","side","action","settle_s","destructive","resource","setup_group",
        "requires","unit","found","source"}``；``source`` ∈
        ``{"bindings_table","fallback","unsupported"}``。未命中（表与基础集都无）时
        ``found=False``、``action="unsupported"``（fallback，交由调用方留痕）。

    Raises:
        BindingSpecError: 命中真实表但 YAML 损坏 / 结构非法（fail-fast）。
    """
    key = str(kind or "").strip()
    sd = str(side or "").strip()
    index, source = load_bindings(bindings_path=bindings_path, settings=settings)

    entry = index.get((key, sd))
    if entry is not None:
        return {**entry, "found": True, "source": source}

    fallback = FALLBACK_BINDINGS.get((key, sd))
    if fallback is not None:
        return {**fallback, "found": True, "source": "fallback"}

    return {
        "kind": key,
        "side": sd,
        "action": "unsupported",
        "settle_s": 0.0,
        "destructive": False,
        "resource": [],
        "setup_group": "",
        "requires": [],
        "unit": "",
        "found": False,
        "source": "unsupported",
    }
