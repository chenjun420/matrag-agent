"""MATRag gateway MCP-HTTP 客户端（只读白名单 + 显式黑名单双轨）。

沿用 ATEStudio ``aterag_agent.py`` 模式：白名单列出允许工具，黑名单显式记录
被拒工具及理由。任何不在白名单（或命中黑名单）的工具调用一律 fail-fast 拒绝
——Agent 不能借道 gateway 触发写入或副作用（红线，spec §4.1 / R7）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from matrag_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: 允许 Agent 调用的 MATRag gateway 只读工具（Phase B.1 骨架，后续按需扩充）。
ALLOWED_TOOLS = frozenset(
    {
        # --- 评审面 ---
        "get_condition_detail",
        "list_pending_review",
        "get_coverage_summary",
        # --- 目录 / 规格查询 ---
        "list_models",
        "list_domain_rules",
        "search_requirements",
        # --- 应答合成（B.4：debug_agent 检索已知 root cause patterns）---
        "ask",
        "query_parameters",
        "get_test_cases",
        "search_cases",
        "get_fixture_spec",
        # --- 对已存事实的计算（不落库）---
        "calculate",
        "validate_constraints",
        "optimize_process",
        "extract_test_conditions",
        "health",
    }
)

#: 被显式拒绝的写 / 副作用工具（工具名 → 理由），与白名单互斥。
DENIED_TOOLS = {
    "ingest_document": "导入规格书并构建型号 KB（写）",
    "build_domain_kb": "构建/更新共享产品类 KB（写）",
    "add_knowledge": "写入公共知识（写；Phase C.1 经人工闸门，不由 Agent 直调）",
}


class GatewayUnavailableError(RuntimeError):
    """gateway 不可达或返回错误 —— fail-fast，不降级为假数据。"""


class ToolNotAllowedError(PermissionError):
    """工具不在白名单（或被显式拒绝）—— 拒绝执行。"""


class GatewayClient:
    """HTTP JSON-RPC 客户端；:meth:`call_tool` 前强制白名单校验。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._url = url or self._settings.gateway_url
        self._api_key = api_key if api_key is not None else self._settings.gateway_api_key
        self._timeout = timeout or self._settings.request_timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    @property
    def url(self) -> str:
        """gateway MCP 端点 URL。"""
        return self._url

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, transport=self._transport)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def check_tool(name: str) -> None:
        """校验工具名；不在白名单或命中黑名单则抛 :class:`ToolNotAllowedError`。"""
        if name in DENIED_TOOLS:
            raise ToolNotAllowedError(f"工具 {name!r} 被显式拒绝：{DENIED_TOOLS[name]}")
        if name not in ALLOWED_TOOLS:
            raise ToolNotAllowedError(f"工具 {name!r} 不在白名单；允许: {sorted(ALLOWED_TOOLS)}")

    def call_tool(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """调用只读 MCP 工具，返回已解析的 ``result``。

        Raises:
            ToolNotAllowedError: 工具未授权。
            GatewayUnavailableError: 网络/HTTP 失败或服务端返回 JSON-RPC 错误。
        """
        self.check_tool(name)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": kwargs},
        }
        try:
            resp = self._http().post(self._url, json=payload, headers=self._headers())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GatewayUnavailableError(
                f"无法连接 MATRag gateway ({self._url}) 调用 {name!r}: {exc}"
            ) from exc
        return _parse_mcp_response(resp, name)


def _parse_mcp_response(resp: httpx.Response, name: str) -> dict[str, Any]:
    """解析 MCP 响应：兼容 application/json 与 text/event-stream 两种返回。"""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        payloads = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
        if not payloads:
            raise GatewayUnavailableError(f"gateway 返回空 SSE 响应（{name!r}）")
        data: Any = json.loads(payloads[-1])
    else:
        data = resp.json()

    if isinstance(data, dict) and data.get("error"):
        raise GatewayUnavailableError(f"gateway 工具 {name!r} 返回错误: {data['error']}")
    result = data.get("result", data) if isinstance(data, dict) else data
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    if not isinstance(result, dict):
        return {"result": result}
    return result
