"""调 ATEStudio ``/aterag/plan``，返回 plan_yaml + gaps + pending + warnings 四键。

纯函数包装 :class:`AteStudioClient`；HTTP 错误由 client 抛（fail-fast，
不在此吞错）。B.3（D16）在此暴露 planner 的 ``warnings``（资源冲突 / 占位限制），
与 ``plan_pending``（未签字 draft）/ ``plan_gaps``（缺判据）三类分离供解读。
"""

from __future__ import annotations

from typing import Any

from matrag_agent.clients.atestudio import AteStudioClient


def plan(bundle: dict[str, Any], *, client: AteStudioClient | None = None) -> dict[str, Any]:
    """请求 ATEStudio 生成 plan，并归一化为三类解读字段。

    Args:
        bundle: MATRag 导出的 bundle 对象。
        client: 显式客户端（测试可注入 stub）；缺省时构建默认客户端。

    Returns:
        ``{"plan_yaml": str, "plan_gaps": list, "plan_pending": list, "warnings": list}``。
    """
    cli = client or AteStudioClient()
    raw = cli.plan_review(bundle)
    return {
        "plan_yaml": raw.get("plan_yaml") or raw.get("yaml") or "",
        "plan_gaps": list(raw.get("plan_gaps") or raw.get("gaps") or []),
        "plan_pending": list(raw.get("plan_pending") or raw.get("pending") or []),
        "warnings": list(raw.get("warnings") or []),
    }
