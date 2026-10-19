"""matrag-agent 配置：环境变量驱动（``MATRAG_AGENT_`` 前缀，``.env`` 可覆盖）。

设计原则与 MATRag 主仓一致：任何"默认写死"的上游选择都必须经此读取，
便于测试用 stub 覆盖，禁止业务代码散落硬编码 URL / Token / 端口。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: 仓库根目录（``<repo>/src/matrag_agent/config.py`` 向上三级）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: MATRag 主仓默认位置（兄弟目录），用于读取 ``data/exports/*.bundle.json``。
DEFAULT_MATRAG_ROOT = REPO_ROOT.parent / "MATRag"


class Settings(BaseSettings):
    """matrag-agent 运行时配置。

    所有字段可由环境变量覆盖（前缀 ``MATRAG_AGENT_``），例如
    ``MATRAG_AGENT_LLM_MODEL`` 覆盖 :attr:`llm_model`。
    """

    model_config = SettingsConfigDict(
        env_prefix="MATRAG_AGENT_", env_file=REPO_ROOT / ".env", extra="ignore"
    )

    profile: str = "dev"  # dev | prod

    # ---- LLM（Pydantic AI，OpenAI 兼容端点）----
    llm_base: str = ""
    llm_api_key: str = ""
    llm_model: str = "qwen3.7-flash"
    llm_temperature: float = 0.0
    llm_timeout: float = 60.0
    llm_max_tokens: int = 4096
    # 平台兼容参数：qwen thinking 模式拒绝 tool_choice=required，本服务恒关闭。
    llm_enable_thinking: bool = False

    # ---- MATRag gateway（MCP over HTTP）----
    gateway_url: str = "http://127.0.0.1:8080/mcp"
    gateway_api_key: str = ""
    request_timeout: float = 60.0

    # ---- ATEStudio（长期 JWT，Phase 0 D14 产物）----
    atestudio_base_url: str = "http://127.0.0.1:8000"
    atestudio_jwt_token: str = ""

    # ---- MATRag 产物路径（bundle 导出）----
    matrag_root: Path = DEFAULT_MATRAG_ROOT
    exports_dir: Path | None = None  # None → ``matrag_root/data/exports``

    # ---- service ----
    service_host: str = "127.0.0.1"
    service_port: int = 8322

    @property
    def live(self) -> bool:
        """真实外部调用是否可用（测试用）：LLM 端点齐备才算 live。"""
        return bool(self.llm_base and self.llm_api_key and self.llm_model)

    @property
    def resolved_exports_dir(self) -> Path:
        """bundle 导出目录；未显式配置时回退到 ``matrag_root/data/exports``。"""
        return self.exports_dir or (self.matrag_root / "data" / "exports")


@lru_cache
def get_settings() -> Settings:
    """进程内单例（测试可用 ``get_settings.cache_clear()`` 重置）。"""
    return Settings()


#: 便捷单例（测试可 monkeypatch 字段）。
settings = get_settings()
