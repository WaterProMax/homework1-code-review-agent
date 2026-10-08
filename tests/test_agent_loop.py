from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from code_review_agent.agent import CodeReviewAgent
from code_review_agent.config import Settings
from code_review_agent.memory import MemoryStore


class FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, tools=None, tool_choice=None, temperature=0.1):
        self.calls += 1
        if self.calls == 1:
            call = SimpleNamespace(
                id="call_read",
                function=SimpleNamespace(
                    name="read_file",
                    arguments='{"path": "examples/buggy.py"}',
                ),
            )
            message = SimpleNamespace(content="", tool_calls=[call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        message = SimpleNamespace(
            content="## 摘要\n风险等级：高。已读取文件。\n\n## 问题列表\n- 可变默认参数\n",
            tool_calls=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_agent_loop_uses_tool_then_finishes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    settings = Settings(
        api_key="test",
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        session_dir=tmp_path / "sessions",
    )
    memory = MemoryStore("loop", settings.session_dir, 4000, 4)
    agent = CodeReviewAgent(settings, memory, llm=FakeLLM())
    result = agent.run("请审查 examples/buggy.py")
    assert result.stop_reason == "final"
    assert result.tools_used == ["read_file"]
    assert "风险等级" in result.reply
    assert memory.path.exists()
    overlay = memory.overlay_text()
    assert "buggy.py" in overlay
