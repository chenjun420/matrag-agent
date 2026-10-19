"""调 ATEStudio ``failure_indexer.audit_report``（B.4.3）—— execution 失败链路审计。

纯函数包装 :class:`AteStudioClient.audit_report`：HTTP 错误由 client 抛（fail-fast，
不在此吞错）；本模块只做入口参数闸门与返回形状校验，**不写库、不订阅 NATS**。

对应 ATEStudio ``services/failure_indexer.audit_report``（Phase A.3 纯函数）：给定
``execution_id`` 返回 ``execution_status`` / ``used_requirement_codes`` /
``measured_conditions`` / ``failed_steps`` / ``pending_signoffs`` 等，供 debug_agent
结合 RAG 上下文做根因综合。
"""

from __future__ import annotations

from typing import Any

__all__ = ["QueryExecError", "query_exec", "AUDIT_ENDPOINT"]

#: 服务端审计端点（与 :class:`AteStudioClient.audit_report` 一致，供审计留痕）。
AUDIT_ENDPOINT = "/api/v1/executions/audit"


class QueryExecError(RuntimeError):
    """execution 审计查询失败（缺 client / 缺 id / 返回非法）—— fail-fast。"""


async def query_exec(atestudio_client: Any, execution_id: str) -> dict:
    """调 ATEStudio 取 ``execution_id`` 的 failure_indexer 审计报告。

    Args:
        atestudio_client: ATEStudio 客户端（须提供 ``audit_report(execution_id)``）；
            测试注入 stub。
        execution_id: 执行记录 id（= run_id）。

    Returns:
        审计报告 dict（原样透出，字段见 ATEStudio ``failure_indexer.audit_report``）。

    Raises:
        QueryExecError: 未提供 client / ``execution_id`` 为空 / 返回非对象或带 ``error``。
        matrag_agent.clients.atestudio.AteStudioUnavailableError: client 侧 HTTP 失败
            （不在此吞错，降级由 :mod:`matrag_agent.agents.debug_agent` 统一留痕）。
    """
    if atestudio_client is None:
        raise QueryExecError("atestudio_client 未提供，无法查询 execution 审计")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise QueryExecError("execution_id 不能为空")

    report = atestudio_client.audit_report(execution_id)
    if not isinstance(report, dict):
        raise QueryExecError(
            f"审计报告必须是 JSON 对象，got {type(report).__name__}"
        )
    if report.get("error"):
        raise QueryExecError(f"审计查询返回错误: {report['error']}")
    return report
