"""pytest 全局配置：离线优先；live 用例按 ``settings.live`` 跳过。"""

from __future__ import annotations

import pytest

from matrag_agent.config import Settings


@pytest.fixture
def make_settings():
    """返回构造「脱离 .env」的 :class:`Settings` 工厂（测试注入用）。

    默认 ``_env_file=None`` 关闭 ``.env`` 读取，仅保留显式入参，
    保证单元测试可复现、不依赖本机环境。
    """

    def _make(**kwargs: object) -> Settings:
        return Settings(_env_file=None, **kwargs)

    return _make
