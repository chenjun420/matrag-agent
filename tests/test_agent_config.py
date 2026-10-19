"""config.py 单元测试：默认值、env 覆盖、live 判定、exports 目录解析。"""

from __future__ import annotations

from pathlib import Path

from matrag_agent.config import Settings


def test_defaults_model_and_port() -> None:
    s = Settings(_env_file=None)
    assert s.llm_model == "qwen3.7-flash"
    assert s.llm_enable_thinking is False
    assert s.service_port == 8322


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("MATRAG_AGENT_LLM_MODEL", "qwen-custom")
    monkeypatch.setenv("MATRAG_AGENT_SERVICE_PORT", "9000")
    s = Settings(_env_file=None)
    assert s.llm_model == "qwen-custom"
    assert s.service_port == 9000


def test_live_requires_full_llm_endpoint() -> None:
    assert Settings(_env_file=None).live is False
    full = Settings(_env_file=None, llm_base="http://x", llm_api_key="k", llm_model="m")
    assert full.live is True


def test_resolved_exports_dir_default() -> None:
    s = Settings(_env_file=None, matrag_root=Path("R"))
    assert s.resolved_exports_dir == Path("R") / "data" / "exports"


def test_resolved_exports_dir_override(tmp_path) -> None:
    s = Settings(_env_file=None, exports_dir=tmp_path)
    assert s.resolved_exports_dir == tmp_path
