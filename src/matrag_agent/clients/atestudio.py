"""ATEStudio HTTP 客户端（长期 JWT；端点白名单）。

Token 来自环境变量 ``ATESTUDIO_JWT_TOKEN``（Phase 0 D14 service-account 产物）；
所有请求带 ``Authorization: Bearer``。端点必须命中白名单，否则 fail-fast
——Agent 不得任意访问 ATEStudio 的写/管理端点（spec §4.4 跨仓依赖矩阵）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from matrag_agent.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: 允许调用的 ATEStudio 端点白名单。
ALLOWED_ENDPOINTS = frozenset(
    {
        "/api/v1/imports/aterag/plan",  # bundle → plan（只读推导）
        "/api/v1/imports/aterag",  # bundle 导入（dry_run 默认真；写需显式确认）
        "/api/v1/executions/audit",  # B.4：execution_id → failure_indexer 审计（只读）
    }
)

#: 长期 JWT 的环境变量名（无前缀；Phase 0 由 ATEStudio 侧签发）。
TOKEN_ENV = "ATESTUDIO_JWT_TOKEN"


class AteStudioUnavailableError(RuntimeError):
    """ATEStudio 不可达或返回错误 —— fail-fast，不降级。"""


class EndpointNotAllowedError(PermissionError):
    """端点不在白名单 —— 拒绝访问。"""


class AteStudioAuthError(RuntimeError):
    """缺少 JWT —— 拒绝无鉴权调用。"""


class AteStudioClient:
    """ATEStudio HTTP 客户端：白名单端点 + Bearer JWT 鉴权。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = (base_url or self._settings.atestudio_base_url).rstrip("/")
        self._token = token or os.getenv(TOKEN_ENV, "") or self._settings.atestudio_jwt_token
        self._timeout = timeout or self._settings.request_timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    @property
    def base_url(self) -> str:
        """ATEStudio 基地址（无尾斜杠）。"""
        return self._base_url

    @property
    def has_token(self) -> bool:
        """是否已取得 JWT。"""
        return bool(self._token)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, transport=self._transport)
        return self._client

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise AteStudioAuthError(
                f"缺少 ATEStudio JWT（环境变量 {TOKEN_ENV}）—— 拒绝无鉴权调用"
            )
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def check_endpoint(path: str) -> None:
        """校验端点路径；不在白名单则抛 :class:`EndpointNotAllowedError`。"""
        if path not in ALLOWED_ENDPOINTS:
            raise EndpointNotAllowedError(
                f"端点 {path!r} 不在白名单；允许: {sorted(ALLOWED_ENDPOINTS)}"
            )

    def _post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """白名单校验 + 带 JWT 的 POST，返回解析后的 JSON 对象。"""
        self.check_endpoint(path)
        headers = self._headers()
        try:
            resp = self._http().post(
                self._base_url + path, params=params, json=bundle, headers=headers
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise AteStudioUnavailableError(
                f"ATEStudio ({self._base_url}{path}) 调用失败: {exc}"
            ) from exc
        data = resp.json()
        if not isinstance(data, dict):
            raise AteStudioUnavailableError(
                f"ATEStudio ({path}) 返回非对象 JSON: {type(data).__name__}"
            )
        return data

    def plan_review(self, bundle: dict[str, Any]) -> dict[str, Any]:
        """调 ``/api/v1/imports/aterag/plan``，返回 plan 结果原始对象。"""
        return self._post("/api/v1/imports/aterag/plan", bundle=bundle)

    def import_bundle(
        self,
        bundle: dict[str, Any],
        dry_run: bool = True,
        confirm_overwrite: bool = False,
    ) -> dict[str, Any]:
        """调 ``/api/v1/imports/aterag``；默认 ``dry_run=True``（安全默认）。"""
        params = {"dry_run": dry_run, "confirm_overwrite": confirm_overwrite}
        return self._post("/api/v1/imports/aterag", params=params, bundle=bundle)

    def audit_report(self, execution_id: str) -> dict[str, Any]:
        """调 ``/api/v1/executions/audit``，返回 failure_indexer 审计报告原始对象。

        对应 ATEStudio ``services/failure_indexer.audit_report``（Phase A.3 纯函数：
        只读 Execution + step_results + test_conditions，不写库）的服务端入口。
        端点形态为 ``POST /api/v1/executions/audit``，``execution_id`` 放在
        **JSON 请求体**中（服务端 ``ExecutionAuditRequest``）；端点路径保持静态，
        便于白名单精确校验。缺 ``execution_id`` 时服务端返回 422（fail-fast）。

        Args:
            execution_id: 执行记录 id（= run_id）。

        Returns:
            审计报告 dict（``execution_id`` / ``execution_status`` /
            ``used_requirement_codes`` / ``measured_conditions`` / ``failed_steps`` /
            ``pending_signoffs`` 等字段）。

        Raises:
            AteStudioAuthError: 缺 JWT。
            EndpointNotAllowedError: 端点不在白名单（不应发生）。
            AteStudioUnavailableError: HTTP / 解析失败（fail-fast，降级走上层）。
        """
        return self._post(
            "/api/v1/executions/audit", bundle={"execution_id": execution_id}
        )
