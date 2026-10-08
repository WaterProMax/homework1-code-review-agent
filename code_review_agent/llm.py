from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from code_review_agent.config import Settings


RETRYABLE = (APIConnectionError, APITimeoutError, RateLimitError)


class LLMError(RuntimeError):
    """LLM 调用失败。"""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
        )

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.1,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        last_error: Exception | None = None
        for attempt in range(1, self._settings.max_retries + 1):
            try:
                return self._client.chat.completions.create(**kwargs)
            except RETRYABLE as exc:
                last_error = exc
                if attempt >= self._settings.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 4))
            except APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and status >= 500 and attempt < self._settings.max_retries:
                    last_error = exc
                    time.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise LLMError(f"LLM 请求失败: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise LLMError(f"LLM 请求失败: {exc}") from exc
        raise LLMError(f"LLM 多次重试后仍失败: {last_error}") from last_error
