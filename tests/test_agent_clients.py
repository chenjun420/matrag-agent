"""clients 单元测试：LLM / gateway / atestudio（全部离线 stub）。"""

from __future__ import annotations

import json

import httpx
import pytest

from matrag_agent.clients.atestudio import (
    ALLOWED_ENDPOINTS,
    AteStudioAuthError,
    AteStudioClient,
    EndpointNotAllowedError,
)
from matrag_agent.clients.gateway import (
    ALLOWED_TOOLS,
    DENIED_TOOLS,
    GatewayClient,
    GatewayUnavailableError,
    ToolNotAllowedError,
)
from matrag_agent.clients.llm import LLMClient, LLMNotConfiguredError
from matrag_agent.config import settings


# ---------------------------------------------------------------- LLM ----
def test_llm_unconfigured(make_settings) -> None:
    assert LLMClient(make_settings()).configured is False


def test_llm_configured_with_endpoint(make_settings) -> None:
    client = LLMClient(make_settings(llm_base="http://x", llm_api_key="k", llm_model="qwen3.7-flash"))
    assert client.configured is True


def test_llm_init_does_not_build_model(make_settings) -> None:
    """``__init__`` 不构建 client（与 MATRag llm.py 一致）。"""
    client = LLMClient(make_settings())
    assert client._model is None


def test_llm_complete_fails_closed(make_settings) -> None:
    with pytest.raises(LLMNotConfiguredError):
        LLMClient(make_settings()).complete("system", "user")


# ------------------------------------------------------------ gateway ----
def test_gateway_allow_deny_disjoint() -> None:
    """白名单与黑名单互斥（双轨一致性）。"""
    assert ALLOWED_TOOLS.isdisjoint(DENIED_TOOLS)


def test_gateway_call_tool_ok(make_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})

    gw = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    assert gw.call_tool("health") == {"ok": True}


def test_gateway_call_tool_denied(make_settings) -> None:
    with pytest.raises(ToolNotAllowedError):
        GatewayClient(make_settings()).call_tool("ingest_document")


def test_gateway_call_tool_not_whitelisted(make_settings) -> None:
    with pytest.raises(ToolNotAllowedError):
        GatewayClient(make_settings()).call_tool("definitely_not_a_tool")


def test_gateway_unavailable_maps_error(make_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    gw = GatewayClient(make_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(GatewayUnavailableError):
        gw.call_tool("health")


def test_gateway_sends_bearer_when_api_key(make_settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"result": {}})

    gw = GatewayClient(make_settings(gateway_api_key="secret"), transport=httpx.MockTransport(handler))
    gw.call_tool("health")
    assert seen["auth"] == "Bearer secret"


# ---------------------------------------------------------- atestudio ----
def test_atestudio_endpoint_whitelist_contents() -> None:
    assert "/api/v1/imports/aterag/plan" in ALLOWED_ENDPOINTS
    assert "/api/v1/imports/aterag" in ALLOWED_ENDPOINTS


def test_atestudio_plan_review_ok(make_settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        body = {"plan_yaml": "steps: []", "plan_pending": [], "plan_gaps": []}
        return httpx.Response(200, json=body)

    cli = AteStudioClient(make_settings(), token="tok", transport=httpx.MockTransport(handler))
    out = cli.plan_review({"product_code": "PA601"})
    assert out["plan_yaml"] == "steps: []"
    assert seen["auth"] == "Bearer tok"
    assert seen["url"].endswith("/api/v1/imports/aterag/plan")


def test_atestudio_requires_token(make_settings, monkeypatch) -> None:
    monkeypatch.delenv("ATESTUDIO_JWT_TOKEN", raising=False)
    cli = AteStudioClient(
        make_settings(atestudio_jwt_token=""),
        token="",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    with pytest.raises(AteStudioAuthError):
        cli.plan_review({})


def test_atestudio_endpoint_not_allowed(make_settings) -> None:
    cli = AteStudioClient(make_settings(), token="t")
    with pytest.raises(EndpointNotAllowedError):
        cli._post("/api/v1/evil", bundle={})


def test_atestudio_import_bundle_params(make_settings) -> None:
    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"imported": 3})

    cli = AteStudioClient(make_settings(), token="t", transport=httpx.MockTransport(handler))
    out = cli.import_bundle({"a": 1}, dry_run=True, confirm_overwrite=False)
    assert out == {"imported": 3}
    assert seen["query"] == {"dry_run": "true", "confirm_overwrite": "false"}


def test_atestudio_audit_report_posts_execution_id_body(make_settings) -> None:
    """audit_report 走 POST 静态端点，execution_id 放请求体（非查询参数）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"execution_id": "exec-123", "execution_status": "FAILED"})

    cli = AteStudioClient(make_settings(), token="t", transport=httpx.MockTransport(handler))
    out = cli.audit_report("exec-123")
    assert out["execution_status"] == "FAILED"
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/v1/executions/audit")
    assert "?" not in seen["url"]  # execution_id 不经查询参数
    assert json.loads(seen["body"]) == {"execution_id": "exec-123"}


def test_atestudio_unavailable_maps_error(make_settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    cli = AteStudioClient(make_settings(), token="t", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        cli.plan_review({})


# -------------------------------------------------------------- live ----
@pytest.mark.skipif(not settings.live, reason="需要真实 LLM 端点（MATRAG_AGENT_LLM_*）")
def test_llm_live_complete() -> None:
    out = LLMClient(settings).complete("你是测试助手。", "只回复两个字母: ok", max_tokens=16)
    assert isinstance(out, str)
