"""调 MATRag gateway ``/ask``（B.4）—— 检索已知 root cause patterns（只读）。

包装 :meth:`GatewayClient.call_tool`：经 gateway MCP 工具 ``ask`` 检索（spec §5 B.4
「RAG 检索（已知 root cause patterns）」）。本模块只做参数闸门 + 命中归一，**不写库、
不重解析规格书**（规格书 single source of truth 在 MATRag 主仓）。

归一契约：无论 gateway 返回 ``chunks`` / ``hits``（``search_context`` 形态）还是
``citations``（``ask`` 形态），统一产出 chunk 列表，每项含 ``text`` / ``source`` /
``score`` 三键（外加 ``chunk_id`` / ``doc_id`` 供证据回溯）。检索失败 fail-fast 抛
:class:`RagSearchError`，由调用方（debug_agent）统一降级留痕，绝不静默吞错。
"""

from __future__ import annotations

from typing import Any

__all__ = ["RagSearchError", "RAG_TOOL", "rag_search"]

#: 使用的 MATRag gateway MCP 工具名（应答合成；检索 + 证据合成 + 行级引用）。
RAG_TOOL = "ask"


class RagSearchError(RuntimeError):
    """RAG 检索失败（缺 client / 空 query / 非法 top_k / gateway 返回错误）—— fail-fast。"""


def _coerce_score(value: Any) -> float:
    """宽松取分数：非数值（含 bool）退化为 ``0.0``（得分缺失不致命）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _chunk_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """把一条命中/引用归一为 ``{text, source, score, chunk_id, doc_id}``。

    兼容 ``search_context`` 的 hit（``text`` + ``anchor`` + ``score``）与 ``ask`` 的
    citation（``section_path`` + ``doc_id`` + ``score``，无正文时退回小节路径/标记）。
    无正文也无来源的条目直接丢弃（不产出空 chunk）。
    """
    anchor = item.get("anchor") if isinstance(item.get("anchor"), dict) else {}
    doc_id = str(item.get("doc_id") or anchor.get("doc_id") or "")
    section = str(item.get("section_path") or anchor.get("section_path") or "")
    chunk_id = str(item.get("chunk_id") or anchor.get("chunk_id") or "")
    text = str(item.get("text") or "") or section or str(item.get("marker") or "")
    source = str(item.get("source") or "") or doc_id or section
    if not text and not source:
        return None
    return {
        "text": text,
        "source": source,
        "score": _coerce_score(item.get("score")),
        "chunk_id": chunk_id,
        "doc_id": doc_id,
    }


def _normalize_chunks(raw: Any, *, top_k: int) -> list[dict[str, Any]]:
    """把 gateway 响应归一为 chunk 列表（``chunks`` → ``hits`` → ``citations`` 依次回退）。"""
    if not isinstance(raw, dict):
        raise RagSearchError(f"gateway 返回非对象: {type(raw).__name__}")
    if raw.get("error"):
        raise RagSearchError(f"gateway 检索返回错误: {raw['error']}")

    items: Any = raw.get("chunks")
    if not isinstance(items, list):
        items = raw.get("hits")
    if not isinstance(items, list):
        items = raw.get("citations")
    if not isinstance(items, list):
        return []

    chunks: list[dict[str, Any]] = []
    for item in items[:top_k]:
        if not isinstance(item, dict):
            continue
        chunk = _chunk_from_item(item)
        if chunk is not None:
            chunks.append(chunk)
    return chunks


async def rag_search(
    gateway_client: Any, query: str, *, top_k: int = 5
) -> list[dict[str, Any]]:
    """经 MATRag gateway ``/ask`` 检索已知 root cause patterns（只读）。

    Args:
        gateway_client: MATRag gateway 客户端（须提供 ``call_tool(name, **kwargs)``）；
            测试注入 stub。
        query: 检索查询串（通常由 execution 审计的失败步骤/需求/状态拼成）。
        top_k: 返回上限（正整数）。

    Returns:
        chunk 列表，每项 ``{text, source, score, chunk_id, doc_id}``。

    Raises:
        RagSearchError: 缺 client / 空 query / 非法 top_k / gateway 返回错误对象。
        matrag_agent.clients.gateway.GatewayUnavailableError: client 侧网络/HTTP 失败
            （不在此吞错，降级由 debug_agent 统一留痕）。
    """
    if gateway_client is None:
        raise RagSearchError("gateway_client 未提供，无法检索 root cause patterns")
    if not isinstance(query, str) or not query.strip():
        raise RagSearchError("query 不能为空")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise RagSearchError(f"top_k 必须为正整数: {top_k!r}")

    raw = gateway_client.call_tool(RAG_TOOL, query=query, top_k=top_k)
    return _normalize_chunks(raw, top_k=top_k)
