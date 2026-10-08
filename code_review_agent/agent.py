from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from code_review_agent.config import Settings
from code_review_agent.llm import LLMClient, LLMError
from code_review_agent.memory import MemoryStore
from code_review_agent.prompts import SYSTEM_PROMPT
from code_review_agent.tools import Toolbelt


@dataclass
class AgentResult:
    reply: str
    steps: int
    tools_used: list[str] = field(default_factory=list)
    stop_reason: str = "final"
    findings: list[dict[str, Any]] = field(default_factory=list)


class CodeReviewAgent:
    def __init__(self, settings: Settings, memory: MemoryStore, llm: LLMClient | None = None, *, tools: Toolbelt | None = None, on_event: Callable[[str, str], None] | None = None) -> None:
        self.settings = settings
        self.memory = memory
        self.llm = llm or LLMClient(settings)
        self.tools = tools or Toolbelt(settings, memory)
        self.on_event = on_event or (lambda kind, text: None)

    def run(self, user_input: str) -> AgentResult:
        user_input = (user_input or "").strip()
        if not user_input:
            return AgentResult(reply="请输入要审查的文件路径，或贴出代码相关问题。", steps=0, stop_reason="empty")

        self._capture_preferences(user_input)
        self.memory.add_user(user_input)
        self.memory.maybe_compact(self.llm, SYSTEM_PROMPT)

        tools_used: list[str] = []
        last_text = ""
        for step in range(1, self.settings.max_steps + 1):
            messages = self.memory.working_messages(SYSTEM_PROMPT)
            self.on_event("model", f"Agent 第 {step} 步：等待模型选择工具或生成结论")
            try:
                completion = self.llm.complete(messages, tools=self.tools.openai_tools())
            except LLMError as exc:
                fallback = self._fallback_from_tools(tools_used, str(exc))
                self.memory.add_assistant(fallback)
                self._record_episode(user_input, tools_used, fallback)
                self.memory.save()
                return AgentResult(fallback, step, tools_used, stop_reason="llm_error")

            message = completion.choices[0].message
            tool_calls = list(message.tool_calls or [])
            content = (message.content or "").strip()
            last_text = content

            if tool_calls:
                serialized_calls = [_tool_call_to_dict(call) for call in tool_calls]
                self.memory.add_assistant(content or None, tool_calls=serialized_calls)
                for call in tool_calls:
                    name = call.function.name
                    args = call.function.arguments or "{}"
                    self.on_event("tool", f"调用 {name}")
                    observation = self.tools.execute(name, args)
                    self.on_event("observation", f"{name} 返回：{observation[:500]}")
                    tools_used.append(name)
                    self.memory.add_tool(call.id, name, observation)
                continue

            if not content:
                content = "这一轮没有形成审查结论。请补充文件路径，或说明要继续看哪一个 finding。"
            self.memory.add_assistant(content)
            self._record_episode(user_input, tools_used, content)
            self.memory.maybe_compact(self.llm, SYSTEM_PROMPT)
            self.memory.save()
            return AgentResult(content, step, tools_used, stop_reason="final", findings=[self.memory.findings[k].__dict__.copy() for k in sorted(self.tools.touched)])

        forced = last_text or self._fallback_from_tools(tools_used, "达到最大推理步数")
        if "摘要" not in forced:
            forced = (
                "达到最大工具步数，以下基于已收集证据给出阶段性结论。\n\n" + forced
            )
        self.memory.add_assistant(forced)
        self._record_episode(user_input, tools_used, forced)
        self.memory.save()
        return AgentResult(forced, self.settings.max_steps, tools_used, stop_reason="max_steps")

    def _capture_preferences(self, user_input: str) -> None:
        patterns = [
            r"只看[^。；\n]{1,40}",
            r"额外要求[:：][^。；\n]{1,60}",
            r"以后[^。；\n]{0,20}记住[^。；\n]+",
            r"关注(性能|安全|可读性|异常处理|正确性|资源问题?)",
        ]
        for pattern in patterns:
            match = re.search(pattern, user_input)
            if match:
                self.memory.remember_preference(match.group(0))

    def _record_episode(self, user_input: str, tools_used: list[str], reply: str) -> None:
        files = list(self.tools.snapshots.keys()) if self.tools.snapshots is not None else list(self.memory.file_facts.keys())
        finding_ids = [item.finding_id for item in self.memory.findings.values() if item.path in files]
        self.memory.add_episode(
            user_goal=user_input,
            files=files[-6:],
            tools_used=list(dict.fromkeys(tools_used)),
            outcome=reply.splitlines()[0][:200] if reply else "无输出",
            findings=finding_ids[-8:],
        )

    def _fallback_from_tools(self, tools_used: list[str], reason: str) -> str:
        open_findings = [
            item for item in self.memory.findings.values() if item.status in {"open", "confirmed"} and (self.tools.snapshots is None or item.path in self.tools.snapshots)
        ]
        lines = [
            "## 摘要",
            f"风险等级：{'高' if any(item.severity in {'高', 'high'} for item in open_findings) else '中' if open_findings else '低'}。"
            f"模型调用中断（{reason}），下面仅基于已执行工具的观察。",
            "",
            "## 问题列表",
        ]
        if open_findings:
            for item in open_findings[:12]:
                lines.append(
                    f"- **{item.severity}** `{item.path}:{item.location}` {item.title}；证据：{item.evidence or '工具检查'}"
                )
        else:
            lines.append("- 尚未收集到足够证据。请提供 Python 文件路径后重试。")
        lines.extend(
            [
                "",
                "## 改进建议",
                "- 先修复高严重级别问题，再复查。",
                "",
                "## 工具与记忆依据",
                f"- 本轮工具：{', '.join(dict.fromkeys(tools_used)) or '无'}",
                f"- 记忆中未关闭 findings：{len(open_findings)}",
            ]
        )
        return "\n".join(lines)


def _tool_call_to_dict(call: Any) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.function.name,
            "arguments": call.function.arguments or "{}",
        },
    }
