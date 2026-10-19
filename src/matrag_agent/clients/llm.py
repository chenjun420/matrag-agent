"""LLM 门面（Pydantic AI，OpenAI 兼容端点）—— 与 MATRag P3-0 同框架。

所有语义生成（提案 / 解读 / 诊断）只允许经此门面，禁止业务代码直连 SDK。
``__init__`` 不构建 client（与 MATRag ``llm.py`` 一致），避免导入即依赖端点可用；
首次调用才构建模型，未配置时 fail-fast 抛 :class:`LLMNotConfiguredError`。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from matrag_agent.config import Settings, get_settings


class LLMNotConfiguredError(RuntimeError):
    """LLM 未配置（缺 base / api_key / model）—— fail-closed，拒绝假输出。"""


class LLMClient:
    """Pydantic AI 门面（model = ``qwen3.7-flash``，``enable_thinking=False``）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._model: Any | None = None

    @property
    def configured(self) -> bool:
        """LLM 端点是否齐备（仿 MATRag ``llm.py:LLMClient.configured``）。"""
        return bool(
            self._settings.llm_base
            and self._settings.llm_api_key
            and self._settings.llm_model
        )

    def _build_model(self) -> Any:
        """按 settings 构建 OpenAI 兼容模型（Pydantic AI 2.x；显式关闭思考）。

        Raises:
            LLMNotConfiguredError: LLM 未配置（fail-closed）。
        """
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        from pydantic_ai.settings import ModelSettings

        if not self.configured:
            raise LLMNotConfiguredError(
                "LLM 未配置 (MATRAG_AGENT_LLM_BASE/API_KEY/MODEL) —— fail-closed, 拒绝假输出"
            )
        if self._model is None:
            provider = OpenAIProvider(
                base_url=self._settings.llm_base,
                api_key=self._settings.llm_api_key,
            )
            model_settings = ModelSettings(
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                extra_body={"enable_thinking": bool(self._settings.llm_enable_thinking)},
            )
            self._model = OpenAIChatModel(
                self._settings.llm_model, provider=provider, settings=model_settings
            )
        return self._model

    def build_model(self) -> Any:
        """公开的模型构建入口（供 agent 侧构造 Pydantic AI Agent）。

        Returns:
            与 :meth:`_build_model` 相同的模型对象（进程内缓存）。

        Raises:
            LLMNotConfiguredError: LLM 未配置（fail-closed）。
        """
        return self._build_model()

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """单轮补全，返回文本；首次调用才构建模型（fail-fast）。"""
        from pydantic_ai import Agent
        from pydantic_ai.settings import ModelSettings

        model = self._build_model()
        run_settings = ModelSettings(
            temperature=self._settings.llm_temperature if temperature is None else temperature,
            max_tokens=self._settings.llm_max_tokens if max_tokens is None else max_tokens,
            extra_body={"enable_thinking": bool(self._settings.llm_enable_thinking)},
        )
        agent: Any = Agent(model, output_type=str, system_prompt=system)
        result = agent.run_sync(user, model_settings=run_settings)
        return str(result.output)

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        schema_hint: str = "",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """要求模型输出 JSON 对象；解析失败附带原始输出重试一次。"""
        hint = (
            f"\n\n输出 JSON，schema 如下（只输出 JSON，无解释）:\n{schema_hint}"
            if schema_hint
            else ""
        )
        raw = self.complete(system, user + hint, max_tokens=max_tokens)
        try:
            return _loads_lenient(raw)
        except json.JSONDecodeError as exc:
            repair_user = (
                f"上一次输出不是合法 JSON: {exc}\n原始输出:\n{raw[:2000]}\n"
                "请只重输出修正后的 JSON。"
            )
            raw2 = self.complete(system, repair_user + hint, max_tokens=max_tokens)
            return _loads_lenient(raw2)


def _loads_lenient(raw: str) -> dict[str, Any]:
    """容错解析：剥离 ```json 围栏 / 截取首 ``{`` 到末 ``}``。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise json.JSONDecodeError("响应中无 JSON 对象", text or "<empty>", 0)
    return json.loads(text[start : end + 1])


@lru_cache
def get_llm() -> LLMClient:
    """进程内单例门面。"""
    return LLMClient(get_settings())
