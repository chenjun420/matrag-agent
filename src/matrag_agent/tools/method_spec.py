"""读 MATRag ``power_test_methods.yaml`` 取方法四字段（parameters/instruments/interfaces/metrics）。

Phase B.5 ``dev_scripts_agent`` 的只读数据源之一：规格书「方法层」（通用知识）在 MATRag
主仓 ``data/domain/source/power_test_methods.yaml``。本模块只做解析 + 归一，**不写库、
不重解析规格书**（single source of truth 在主仓）。

纪律：文件缺失 / YAML 非法 / 顶层结构不对 → fail-fast 抛 :class:`MethodSpecError`（不降级为
假方法）；**方法 id 未找到**则返回带 ``found=False`` 的空规格（fallback，由调用方留痕）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from matrag_agent.config import Settings, get_settings

__all__ = [
    "DEFAULT_METHODS_RELPATH",
    "METHOD_FIELDS",
    "MethodSpecError",
    "get_method_spec",
    "load_methods",
]

#: 方法四字段（与 ``power_test_methods.yaml`` 的字段名逐字一致）。
METHOD_FIELDS: tuple[str, ...] = ("parameters", "instruments", "interfaces", "metrics")

#: 相对 MATRag 根目录的方法库路径。
DEFAULT_METHODS_RELPATH = Path("data") / "domain" / "source" / "power_test_methods.yaml"


class MethodSpecError(RuntimeError):
    """方法库缺失或非法（fail-fast；不降级为假方法）。"""


def _resolve_path(methods_path: Path | None, settings: Settings) -> Path:
    """解析方法库路径：显式路径优先，否则回退 ``matrag_root`` 下的默认位置。"""
    if methods_path is not None:
        return Path(methods_path)
    return Path(settings.matrag_root) / DEFAULT_METHODS_RELPATH


def _coerce_str_list(raw: Any) -> list[str]:
    """把 YAML 字段宽松归一为 ``list[str]``（字符串按单项处理；非列表 → 空）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _empty_spec(method_id: str, *, source: str) -> dict[str, Any]:
    """构造未命中方法时的空规格（四字段为空，``found=False``，留痕来源）。"""
    return {
        "method_id": method_id,
        "zh": "",
        "en": "",
        "category": "",
        "parameters": [],
        "instruments": [],
        "interfaces": [],
        "metrics": [],
        "found": False,
        "source": source,
    }


def load_methods(
    *, methods_path: Path | None = None, settings: Settings | None = None
) -> dict[str, dict[str, Any]]:
    """加载方法库并按 ``method_id`` 建索引。

    Raises:
        MethodSpecError: 文件缺失 / YAML 非法 / 顶层不是列表。
    """
    resolved = _resolve_path(methods_path, settings or get_settings())
    if not resolved.is_file():
        raise MethodSpecError(f"方法库不存在: {resolved}")
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # 损坏配置不得静默降级
        raise MethodSpecError(f"方法库 YAML 解析失败: {resolved}: {exc}") from exc
    if not isinstance(data, list):
        raise MethodSpecError(f"方法库顶层必须是列表: {resolved}")

    index: dict[str, dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = str(entry.get("method_id") or "").strip()
        if mid:
            index[mid] = entry
    return index


def get_method_spec(
    method_id: str,
    *,
    methods_path: Path | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """取一个方法的四字段规格（parameters/instruments/interfaces/metrics）。

    Args:
        method_id: 方法 id（如 ``M-RIPPLE-NOISE``）。
        methods_path: 显式方法库路径（默认取 ``matrag_root`` 下的默认位置；测试可注入）。
        settings: 显式配置对象（默认 :func:`get_settings`）。

    Returns:
        ``{"method_id","zh","en","category","parameters","instruments","interfaces",
        "metrics","found","source"}``；``source`` ∈ ``{"yaml","empty_id","not_found"}``。
        方法未找到时不抛错，返回带 ``found=False`` 的空规格（fallback，交由调用方留痕）。

    Raises:
        MethodSpecError: 方法库缺失 / 非法（fail-fast）。
    """
    key = str(method_id or "").strip()
    if not key:
        return _empty_spec(key, source="empty_id")

    index = load_methods(methods_path=methods_path, settings=settings)
    entry = index.get(key)
    if entry is None:
        return _empty_spec(key, source="not_found")

    return {
        "method_id": key,
        "zh": str(entry.get("zh") or ""),
        "en": str(entry.get("en") or ""),
        "category": str(entry.get("category") or ""),
        "parameters": _coerce_str_list(entry.get("parameters")),
        "instruments": _coerce_str_list(entry.get("instruments")),
        "interfaces": _coerce_str_list(entry.get("interfaces")),
        "metrics": _coerce_str_list(entry.get("metrics")),
        "found": True,
        "source": "yaml",
    }
