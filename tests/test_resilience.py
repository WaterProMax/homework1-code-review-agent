from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
from openai import APIConnectionError

from code_review_agent.cli import main
from code_review_agent.config import Settings
from code_review_agent.llm import LLMClient
from code_review_agent.memory import MemoryStore


class FlakyCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com"))
        return SimpleNamespace(ok=True, kwargs=kwargs)


def test_llm_retries_transient_connection_error(monkeypatch) -> None:
    settings = Settings(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        max_retries=2,
    )
    client = LLMClient.__new__(LLMClient)
    client._settings = settings
    completions = FlakyCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr("code_review_agent.llm.time.sleep", lambda _: None)

    result = client.complete([{"role": "user", "content": "test"}])

    assert result.ok is True
    assert completions.calls == 2


def test_reset_does_not_require_api_key(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("CODE_REVIEW_SESSION_DIR", str(tmp_path / "sessions"))
    memory = MemoryStore("reset-me", tmp_path / "sessions", 4000, 4)
    memory.save()

    exit_code = main(["--session", "reset-me", "reset", "--yes"])

    assert exit_code == 0
    assert not memory.path.exists()
    assert "已删除会话记忆" in capsys.readouterr().out
