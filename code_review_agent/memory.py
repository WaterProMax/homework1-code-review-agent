from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from code_review_agent.prompts import SUMMARIZE_PROMPT


def estimate_tokens(text: str) -> int:
    """粗估 token：中英混排用字符数/3，避免为作业引入 tiktoken。"""
    if not text:
        return 0
    return max(1, (len(text) + 2) // 3)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_session_id(session_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id.strip())[:64]
    return cleaned or "default"


@dataclass
class Message:
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_api(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            payload["content"] = self.content
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload


@dataclass
class FileFact:
    path: str
    language: str = "python"
    size_bytes: int = 0
    sha256: str = ""
    summary: str = ""
    last_reviewed_at: str = ""


@dataclass
class Finding:
    finding_id: str
    path: str
    severity: str
    title: str
    status: str = "open"  # open | confirmed | resolved | invalid
    location: str = ""
    evidence: str = ""
    suggestion: str = ""
    source: str = "llm"  # tool | llm | user
    updated_at: str = field(default_factory=_utcnow)
    code: str = ""
    scope: str = "<module>"
    fingerprint: str = ""
    confidence: str = "suspected"
    end_line: int = 0
    version: str = ""


@dataclass
class Episode:
    episode_id: str
    created_at: str
    user_goal: str
    files: list[str]
    tools_used: list[str]
    outcome: str
    findings: list[str]


class MemoryStore:
    """
    四层记忆：

    1. Working Memory：本轮要送给模型的窗口（system/memory overlay + 最近对话）
    2. Episodic Memory：每一次审查的情节摘要（查过什么、结论是什么）
    3. Semantic Memory：跨轮稳定知识（用户偏好、文件事实、findings 状态）
    4. Session Persistence：上述状态落到磁盘，下次 CLI 还能接着问
    """

    def __init__(
        self,
        session_id: str,
        session_dir: Path,
        token_budget: int,
        recent_turns_keep: int,
    ) -> None:
        self.session_id = _safe_session_id(session_id)
        self.session_dir = session_dir
        self.token_budget = token_budget
        self.recent_turns_keep = recent_turns_keep
        self.messages: list[Message] = []
        self.episodes: list[Episode] = []
        self.file_facts: dict[str, FileFact] = {}
        self.findings: dict[str, Finding] = {}
        self.preferences: list[str] = []
        self.semantic_notes: list[str] = []
        self.episode_summaries: list[str] = []
        self.created_at = _utcnow()
        self.updated_at = self.created_at

    @property
    def path(self) -> Path:
        return self.session_dir / f"{self.session_id}.json"

    def add_user(self, content: str) -> None:
        self.messages.append(Message(role="user", content=content))
        self.updated_at = _utcnow()

    def add_assistant(
        self,
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        self.messages.append(Message(role="assistant", content=content, tool_calls=tool_calls))
        self.updated_at = _utcnow()

    def add_tool(self, tool_call_id: str, name: str, content: str) -> None:
        self.messages.append(
            Message(role="tool", content=content, tool_call_id=tool_call_id, name=name)
        )
        self.updated_at = _utcnow()

    def remember_preference(self, text: str) -> None:
        text = text.strip()
        if text and text not in self.preferences:
            self.preferences.append(text)
            self.updated_at = _utcnow()

    def upsert_file_fact(self, fact: FileFact) -> None:
        fact.last_reviewed_at = _utcnow()
        self.file_facts[fact.path] = fact
        self.updated_at = _utcnow()

    def upsert_finding(self, finding: Finding) -> Finding:
        existing = self.findings.get(finding.finding_id)
        if existing is None:
            for item in self.findings.values():
                if item.path == finding.path and ((finding.fingerprint and item.fingerprint == finding.fingerprint) or (not finding.fingerprint and item.title == finding.title and item.location == finding.location)):
                    existing = item
                    break
        if existing:
            existing.severity = finding.severity or existing.severity
            existing.status = finding.status or existing.status
            existing.location = finding.location or existing.location
            existing.evidence = finding.evidence or existing.evidence
            existing.suggestion = finding.suggestion or existing.suggestion
            existing.source = finding.source or existing.source
            for key in ("code", "scope", "fingerprint", "confidence", "end_line", "version"):
                value = getattr(finding, key)
                if value:
                    setattr(existing, key, value)
            existing.updated_at = _utcnow()
            self.updated_at = existing.updated_at
            return existing
        if not finding.finding_id:
            finding.finding_id = f"F{max([int(k[1:]) for k in self.findings if re.fullmatch(r'F\d+', k)] or [0]) + 1:03d}"
        self.findings[finding.finding_id] = finding
        self.updated_at = _utcnow()
        return finding

    def resolve_finding(self, finding_id: str, status: str = "resolved") -> bool:
        item = self.findings.get(finding_id)
        if item is None:
            return False
        item.status = status
        item.updated_at = _utcnow()
        self.updated_at = item.updated_at
        return True

    def add_episode(
        self,
        user_goal: str,
        files: list[str],
        tools_used: list[str],
        outcome: str,
        findings: list[str],
    ) -> Episode:
        episode = Episode(
            episode_id=uuid4().hex[:8],
            created_at=_utcnow(),
            user_goal=user_goal[:240],
            files=files,
            tools_used=tools_used,
            outcome=outcome[:400],
            findings=findings,
        )
        self.episodes.append(episode)
        self.episode_summaries.append(
            f"{episode.created_at} 审查 {', '.join(files) or '未指定文件'}："
            f"{episode.outcome}"
        )
        self.episode_summaries = self.episode_summaries[-12:]
        self.episodes = self.episodes[-20:]
        self.updated_at = episode.created_at
        return episode

    def overlay_text(self) -> str:
        lines = ["## Semantic Memory"]
        if self.preferences:
            lines.append("用户偏好：")
            lines.extend(f"- {item}" for item in self.preferences[-8:])
        else:
            lines.append("- （暂无稳定偏好）")
        if self.file_facts:
            lines.append("已知文件：")
            for fact in list(self.file_facts.values())[-8:]:
                summary = fact.summary or "尚无摘要"
                lines.append(
                    f"- {fact.path} ({fact.language}, {fact.size_bytes} bytes, sha256={fact.sha256[:10] or 'n/a'}) {summary}"
                )
        open_findings = [item for item in self.findings.values() if item.status in {"open", "confirmed"}]
        if open_findings:
            lines.append("未关闭 findings：")
            for item in open_findings[-10:]:
                lines.append(
                    f"- [{item.finding_id}] {item.severity} {item.path}:{item.location} {item.title} ({item.status})"
                )
        if self.semantic_notes:
            lines.append("稳定结论：")
            lines.extend(f"- {note}" for note in self.semantic_notes[-8:])
        lines.append("## Episodic Memory")
        if self.episode_summaries:
            lines.extend(f"- {item}" for item in self.episode_summaries[-6:])
        else:
            lines.append("- （尚无审查情节）")
        return "\n".join(lines)

    def working_messages(self, system_prompt: str) -> list[dict[str, Any]]:
        overlay = Message(
            role="system",
            content="以下是可检索记忆，不是本轮用户原话。冲突时以最新工具观察为准。\n" + self.overlay_text(),
        )
        recent = self._recent_dialog_window()
        packed = [Message(role="system", content=system_prompt), overlay, *recent]
        if self._tokens_of(packed) <= self.token_budget:
            return [item.to_api() for item in packed]
        # Drop whole user turns, never individual tool responses.
        while len(recent) > 1 and self._tokens_of([packed[0], packed[1], *recent]) > self.token_budget:
            next_turn = next((i for i, item in enumerate(recent[1:], 1) if item.role == "user"), None)
            if next_turn is None:
                break
            recent = recent[next_turn:]
        return [item.to_api() for item in [packed[0], packed[1], *recent]]

    def maybe_compact(self, llm: Any | None, system_prompt: str = "") -> bool:
        """窗口过大时，把旧对话折叠进 episodic/semantic，只留最近几轮。"""
        # working_messages() 会先把窗口裁到预算内，不能用它判断是否需要压缩。
        overlay = Message(
            role="system",
            content="以下是可检索记忆，不是本轮用户原话。冲突时以最新工具观察为准。\n"
            + self.overlay_text(),
        )
        raw_window = [Message(role="system", content=system_prompt), overlay, *self.messages]
        if self._tokens_of(raw_window) < self.token_budget:
            return False
        digest = self._fallback_digest()
        if llm is not None:
            try:
                completion = llm.complete(
                    [
                        {"role": "system", "content": SUMMARIZE_PROMPT},
                        {"role": "user", "content": self._transcript_for_summary()},
                    ],
                    temperature=0,
                )
                digest = completion.choices[0].message.content or digest
            except Exception:  # noqa: BLE001
                pass
        self._ingest_digest(digest)
        self.messages = self._recent_dialog_window()
        self.updated_at = _utcnow()
        return True

    def save(self) -> Path:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [asdict(item) for item in self.messages],
            "episodes": [asdict(item) for item in self.episodes],
            "file_facts": {key: asdict(value) for key, value in self.file_facts.items()},
            "findings": {key: asdict(value) for key, value in self.findings.items()},
            "preferences": self.preferences,
            "semantic_notes": self.semantic_notes,
            "episode_summaries": self.episode_summaries,
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        return self.path

    @classmethod
    def load(
        cls,
        session_id: str,
        session_dir: Path,
        token_budget: int,
        recent_turns_keep: int,
    ) -> "MemoryStore":
        store = cls(session_id, session_dir, token_budget, recent_turns_keep)
        if not store.path.exists():
            return store
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        store.created_at = raw.get("created_at", store.created_at)
        store.updated_at = raw.get("updated_at", store.updated_at)
        store.messages = [Message(**item) for item in raw.get("messages", [])]
        store.episodes = [Episode(**item) for item in raw.get("episodes", [])]
        store.file_facts = {key: FileFact(**value) for key, value in raw.get("file_facts", {}).items()}
        store.findings = {key: Finding(**value) for key, value in raw.get("findings", {}).items()}
        store.preferences = list(raw.get("preferences", []))
        store.semantic_notes = list(raw.get("semantic_notes", []))
        store.episode_summaries = list(raw.get("episode_summaries", []))
        return store

    def _recent_dialog_window(self) -> list[Message]:
        if not self.messages:
            return []
        turns = 0
        selected: list[Message] = []
        for message in reversed(self.messages):
            selected.append(message)
            if message.role == "user":
                turns += 1
                if turns >= self.recent_turns_keep:
                    break
        selected.reverse()
        while selected and selected[0].role == "tool":
            selected.pop(0)
        return selected

    def _transcript_for_summary(self) -> str:
        chunks = [self.overlay_text(), "", "## Recent Dialogue"]
        for message in self.messages[-20:]:
            role = message.role
            content = (message.content or "")[:1200]
            if message.tool_calls:
                names = [call.get("function", {}).get("name", "tool") for call in message.tool_calls]
                content = f"调用工具: {', '.join(names)}; {content}"
            chunks.append(f"{role}: {content}")
        return "\n".join(chunks)[:8000]

    def _fallback_digest(self) -> str:
        files = ", ".join(self.file_facts.keys()) or "未知文件"
        open_titles = [item.title for item in self.findings.values() if item.status != "resolved"]
        return (
            "[EPISODIC]\n"
            f"- 近期审查过 {files}，未关闭问题：{'; '.join(open_titles[:8]) or '无'}\n"
            "[SEMANTIC]\n"
            + ("\n".join(f"- {item}" for item in self.preferences[-5:]) or "- 无额外偏好")
            + "\n[DROP]\n- 完整源码与重复工具输出"
        )

    def _ingest_digest(self, digest: str) -> None:
        section = "EPISODIC"
        for raw_line in digest.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            upper = line.upper()
            if upper.startswith("[EPISODIC]"):
                section = "EPISODIC"
                continue
            if upper.startswith("[SEMANTIC]"):
                section = "SEMANTIC"
                continue
            if upper.startswith("[DROP]"):
                section = "DROP"
                continue
            text = line[1:].strip() if line.startswith("-") else line
            if not text:
                continue
            if section == "EPISODIC":
                if text not in self.episode_summaries:
                    self.episode_summaries.append(text)
            elif section == "SEMANTIC":
                if text not in self.semantic_notes:
                    self.semantic_notes.append(text)
        self.episode_summaries = self.episode_summaries[-12:]
        self.semantic_notes = self.semantic_notes[-16:]

    def _tokens_of(self, messages: list[Message]) -> int:
        return sum(estimate_tokens(json.dumps(item.to_api(), ensure_ascii=False)) for item in messages)
