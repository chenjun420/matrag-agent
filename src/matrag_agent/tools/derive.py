"""复用 MATRag P3-0d derive（占位接口）。

Phase B.3 将通过跨仓调用接入 MATRag ``domain`` 的反解能力（Monty + SymPy）；
当前骨架不做真实计算，返回显式的 ``not_implemented`` 标记（留痕，不静默降级）。
"""

from __future__ import annotations

from typing import Any

#: 占位状态标记（调用方可据此判断未实现，而非误当真值）。
STATUS_NOT_IMPLEMENTED = "not_implemented"


def derive(expression: str, variables: dict[str, float] | None = None) -> dict[str, Any]:
    """反解表达式（占位）。

    Args:
        expression: 待求解表达式，如 ``"V = I * R"``。
        variables: 已知量（符号 → 数值）。

    Returns:
        占位结果，``status == "not_implemented"``，``value is None``。
    """
    return {
        "status": STATUS_NOT_IMPLEMENTED,
        "expression": expression,
        "variables": dict(variables or {}),
        "value": None,
        "note": "占位接口：Phase B.3 接入 MATRag P3-0d derive（Monty + SymPy）",
    }
