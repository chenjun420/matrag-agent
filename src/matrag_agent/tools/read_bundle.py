"""读 MATRag 导出的 bundle JSON（``data/exports/{product_code}.bundle.json``）。

纯本地文件读取，无外部依赖；文件缺失或非法时 fail-fast（不静默降级）。
"""

from __future__ import annotations

import json
from pathlib import Path

from matrag_agent.config import Settings, get_settings


def read_bundle(
    product_code: str,
    *,
    exports_dir: Path | None = None,
    settings: Settings | None = None,
) -> dict:
    """读取指定产品编码的 bundle。

    Args:
        product_code: 产品编码（如 ``PA601-D54A``）。
        exports_dir: 显式导出目录（默认取 settings 解析值；测试可注入）。
        settings: 显式配置对象（默认 :func:`get_settings`）。

    Returns:
        bundle 顶层对象。

    Raises:
        FileNotFoundError: bundle 文件不存在。
        ValueError: 文件内容顶层不是 JSON 对象。
    """
    resolved = exports_dir or (settings or get_settings()).resolved_exports_dir
    path = Path(resolved) / f"{product_code}.bundle.json"
    if not path.is_file():
        raise FileNotFoundError(f"bundle 不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"bundle 顶层必须是 JSON 对象: {path}")
    return data
