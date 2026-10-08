from __future__ import annotations

import json
from pathlib import Path

from code_review_agent.config import Settings
from code_review_agent.memory import FileFact, Finding, MemoryStore
from code_review_agent.tools import Toolbelt


def make_memory(tmp_path: Path) -> MemoryStore:
    return MemoryStore("test", tmp_path / "sessions", token_budget=4000, recent_turns_keep=4)


def make_settings() -> Settings:
    return Settings(api_key="test", base_url="https://api.deepseek.com", model="deepseek-chat")


def test_read_file_and_parse_detects_bugs(tmp_path: Path) -> None:
    source = tmp_path / "bad.py"
    source.write_text(
        "def add(x, acc=[]):\n    acc.append(x)\n    return acc\n\nvalue = 1 / 0\n",
        encoding="utf-8",
    )
    memory = make_memory(tmp_path)
    tools = Toolbelt(make_settings(), memory)
    read_payload = json.loads(tools.read_file(str(source)))
    assert read_payload["ok"] is True
    parsed = json.loads(tools.parse_python(str(source)))
    codes = {issue["code"] for issue in parsed["issues"]}
    assert "mutable-default" in codes
    assert "div-zero" in codes
    assert str(source) in memory.file_facts


def test_run_snippet_timeout_and_block(tmp_path: Path) -> None:
    memory = make_memory(tmp_path)
    tools = Toolbelt(make_settings(), memory)
    blocked = json.loads(tools.run_snippet("import os\nos.system('echo hi')"))
    assert blocked["ok"] is False
    blocked_file = json.loads(tools.run_snippet("open('secret.txt').read()"))
    assert blocked_file["ok"] is False
    ok = json.loads(tools.run_snippet("print(1+1)", reason="sanity"))
    assert ok["ok"] is True
    assert "2" in ok["stdout"]


def test_memory_overlay_keeps_findings(tmp_path: Path) -> None:
    memory = make_memory(tmp_path)
    memory.remember_preference("只看高危")
    memory.upsert_file_fact(FileFact(path="a.py", language="python", size_bytes=12, sha256="abc", summary="demo"))
    memory.upsert_finding(
        Finding(finding_id="F001", path="a.py", severity="高", title="可变默认参数", location="1")
    )
    overlay = memory.overlay_text()
    assert "只看高危" in overlay
    assert "F001" in overlay
    memory.add_user("请审查 a.py")
    memory.add_assistant("发现可变默认参数")
    path = memory.save()
    loaded = MemoryStore.load("test", tmp_path / "sessions", 4000, 4)
    assert loaded.findings["F001"].title == "可变默认参数"
    assert path.exists()


def test_syntax_error_is_saved_as_confirmed_finding(tmp_path: Path) -> None:
    source = tmp_path / "syntax_error.py"
    source.write_text("def broken(:\n    pass\n", encoding="utf-8")
    memory = make_memory(tmp_path)
    tools = Toolbelt(make_settings(), memory)

    parsed = json.loads(tools.parse_python(str(source)))

    assert parsed["syntax_ok"] is False
    assert any(item.status == "confirmed" for item in memory.findings.values())
    assert any("语法错误" in item.title for item in memory.findings.values())


def test_compaction_checks_complete_history_before_trimming(tmp_path: Path) -> None:
    memory = MemoryStore(
        "compact",
        tmp_path / "sessions",
        token_budget=500,
        recent_turns_keep=2,
    )
    for index in range(8):
        memory.add_user(f"第 {index} 轮 " + "问题" * 30)
        memory.add_assistant("结论" * 30)

    assert memory.maybe_compact(None, "system prompt") is True
    assert len(memory.messages) <= 4
    assert memory.episode_summaries
