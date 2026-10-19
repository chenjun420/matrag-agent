"""外部服务客户端：MATRag gateway（MCP-HTTP）/ ATEStudio（HTTP+JWT）/ LLM（Pydantic AI）。

跨仓调用一律走 HTTP（spec §4.4），且必须带鉴权；本包集中封装出口，
业务代码只依赖这里的门面，禁止直连具体 SDK / 硬编码地址。
"""

from __future__ import annotations

from matrag_agent.clients.atestudio import AteStudioClient
from matrag_agent.clients.gateway import GatewayClient
from matrag_agent.clients.llm import LLMClient, get_llm

__all__ = ["AteStudioClient", "GatewayClient", "LLMClient", "get_llm"]
