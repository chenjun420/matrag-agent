"""matrag-agent：MATRag 产测 Agent 服务（Phase B.1 骨架）。

独立仓库（决策 D3），通过 HTTP + SSE 对外提供 agent 能力（D7）；
数据一律经 MATRag gateway / ATEStudio HTTP 获取，**不重新解析规格书**
（spec §1.5：规格书 single source of truth 在 MATRag 主仓）。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
