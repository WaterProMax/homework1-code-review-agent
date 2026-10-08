from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from code_review_agent.analysis import analyze_python
from code_review_agent.config import Settings
from code_review_agent.memory import FileFact, Finding, MemoryStore, _utcnow


MAX_TOOL_OUTPUT_CHARS = 12_000

SAFE_SNIPPET_CALLS = {
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "len",
    "list",
    "max",
    "min",
    "print",
    "range",
    "repr",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "type",
    "zip",
    "AssertionError",
    "IndexError",
    "KeyError",
    "RuntimeError",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
}

SAFE_SNIPPET_METHODS = {
    "add",
    "append",
    "clear",
    "copy",
    "count",
    "discard",
    "endswith",
    "extend",
    "get",
    "index",
    "insert",
    "items",
    "join",
    "keys",
    "lower",
    "pop",
    "remove",
    "replace",
    "reverse",
    "sort",
    "split",
    "startswith",
    "strip",
    "update",
    "upper",
    "values",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + f"\n...[truncated {len(text) - limit + 80} chars]"


def _dump(payload: dict[str, Any]) -> str:
    # Always return valid JSON, even when tool output exceeds the context budget.
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(encoded) <= MAX_TOOL_OUTPUT_CHARS:
        return encoded
    def compact(value: Any, limit: int) -> Any:
        if isinstance(value, str):
            return _clip(value, limit) if len(value) > limit else value
        if isinstance(value, list):
            return [compact(item, limit) for item in value[:20]]
        if isinstance(value, dict):
            return {key: compact(item, 8000 if key == "content" else limit) for key, item in value.items()}
        return value
    reduced = compact(payload, 500)
    reduced["truncated"] = True
    encoded = json.dumps(reduced, ensure_ascii=False, indent=2)
    while len(encoded) > MAX_TOOL_OUTPUT_CHARS:
        for key, item in list(reduced.items()):
            if isinstance(item, list) and item:
                reduced[key] = item[:max(0, len(item) // 2)]
            elif isinstance(item, str) and len(item) > 500:
                reduced[key] = _clip(item, max(500, len(item) // 2))
        shorter = json.dumps(reduced, ensure_ascii=False, indent=2)
        if len(shorter) >= len(encoded):
            return json.dumps({"ok": payload.get("ok", True), "truncated": True, "summary": "工具输出过长；完整发现已保存到记忆，请检索具体问题。"}, ensure_ascii=False)
        encoded = shorter
    return encoded


def _resolve_path(path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _validate_snippet(code: str) -> str | None:
    """只允许无导入、无 I/O 的纯 Python 最小片段。

    这不是通用沙箱，但会在启动子进程前拒绝文件、网络、进程和动态执行入口。
    """
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return f"片段语法错误（第 {exc.lineno or 1} 行）: {exc.msg}"

    forbidden_nodes = (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal)
    defined_calls = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    allowed_calls = SAFE_SNIPPET_CALLS | defined_calls

    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes):
            return f"不允许在片段中使用 {type(node).__name__}"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return "不允许访问双下划线名称"
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return "不允许访问双下划线属性"
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            if node.func.id not in allowed_calls:
                return f"不允许调用 {node.func.id}()"
            continue
        if isinstance(node.func, ast.Attribute):
            if node.func.attr not in SAFE_SNIPPET_METHODS:
                return f"不允许调用方法 {node.func.attr}()"
            continue
        return "不允许动态构造可调用对象"
    return None


def _limit_snippet_resources() -> None:
    """POSIX 子进程资源上限；不支持的限制由平台自行忽略。"""
    try:
        import resource
    except ImportError:
        return

    limits = [
        (resource.RLIMIT_CPU, 1),
        (resource.RLIMIT_FSIZE, 1_000_000),
        (resource.RLIMIT_NOFILE, 16),
    ]
    for kind, value in limits:
        try:
            resource.setrlimit(kind, (value, value))
        except (OSError, ValueError):
            pass


class Toolbelt:
    def __init__(self, settings: Settings, memory: MemoryStore, *, root: Path | None = None, snapshots: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.memory = memory
        self.root = root.resolve() if root else None
        self.snapshots = snapshots
        self.touched: set[str] = set()
        self.specs = [
            ToolSpec(
                name="read_file",
                description="读取本地源码文件。审查前必须先读到内容，禁止猜测文件正文。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "相对或绝对文件路径"},
                    },
                    "required": ["path"],
                },
                handler=self.read_file,
            ),
            ToolSpec(
                name="parse_python",
                description="对 Python 源码做 AST 静态检查，发现语法错误和常见坏味道，并返回函数/类结构。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "已读取或待分析的 Python 文件路径"},
                    },
                    "required": ["path"],
                },
                handler=self.parse_python,
            ),
            ToolSpec(
                name="run_snippet",
                description=(
                    "执行经 AST 白名单校验的纯 Python 最小片段，用来验证是否立即异常。"
                    "拒绝导入、I/O、系统调用和动态执行；这仍不是通用容器沙箱。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "最小可执行片段"},
                        "reason": {"type": "string", "description": "为什么要执行这段代码"},
                    },
                    "required": ["code"],
                },
                handler=self.run_snippet,
            ),
            ToolSpec(
                name="record_finding",
                description="登记模型发现。必须给出已读取源码的精确位置与证据；仅登记为待确认，不可自行关闭问题。",
                parameters={"type": "object", "properties": {
                    "path": {"type": "string"}, "line": {"type": "integer"},
                    "title": {"type": "string"}, "evidence": {"type": "string"},
                    "suggestion": {"type": "string"}, "severity": {"type": "string", "enum": ["高", "中", "低"]}
                }, "required": ["path", "line", "title", "evidence", "suggestion", "severity"]},
                handler=self.record_finding,
            ),
            ToolSpec(
                name="remember",
                description="把稳定偏好、已确认事实或 findings 状态写入长期记忆，供后续轮次使用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["preference", "note", "finding"],
                            "description": "preference=用户偏好, note=稳定结论, finding=登记或更新问题",
                        },
                        "content": {"type": "string", "description": "要记住的文本；finding 时可当 title 用"},
                        "path": {"type": "string"},
                        "severity": {"type": "string", "enum": ["高", "中", "低", "high", "medium", "low"]},
                        "status": {
                            "type": "string",
                            "enum": ["open", "confirmed", "resolved", "invalid"],
                        },
                        "location": {"type": "string"},
                        "finding_id": {"type": "string"},
                    },
                    "required": ["kind", "content"],
                },
                handler=self.remember,
            ),
            ToolSpec(
                name="recall_memory",
                description="按关键词检索情节记忆、文件事实和 findings，避免重复全量读文件。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "文件名、finding id、或问题关键词"},
                    },
                    "required": ["query"],
                },
                handler=self.recall_memory,
            ),
        ]
        self._by_name = {item.name: item for item in self.specs}

    def openai_tools(self) -> list[dict[str, Any]]:
        result = []
        for spec in self.specs:
            parameters = copy.deepcopy(spec.parameters)
            if self.root is not None and spec.name == "remember":
                parameters["properties"] = {key: value for key, value in parameters["properties"].items() if key in {"kind", "content"}}
                parameters["properties"]["kind"]["enum"] = ["preference", "note"]
            result.append({"type": "function", "function": {"name": spec.name, "description": spec.description, "parameters": parameters}})
        return result

    def execute(self, name: str, arguments_json: str) -> str:
        spec = self._by_name.get(name)
        if spec is None:
            return _dump({"ok": False, "error": f"未知工具: {name}"})
        try:
            args = json.loads(arguments_json or "{}")
            if not isinstance(args, dict):
                raise ValueError("工具参数必须是 JSON 对象")
        except json.JSONDecodeError as exc:
            return _dump({"ok": False, "error": f"工具参数不是合法 JSON: {exc}"})
        try:
            return spec.handler(**args)
        except TypeError as exc:
            return _dump({"ok": False, "error": f"工具参数不匹配: {exc}"})
        except Exception as exc:  # noqa: BLE001
            return _dump({"ok": False, "error": f"{name} 执行失败: {exc}"})

    def read_file(self, path: str) -> str:
        target = self.resolve_path(path)
        if not target.exists():
            return _dump({"ok": False, "error": f"文件不存在: {target}"})
        if not target.is_file():
            return _dump({"ok": False, "error": f"不是文件: {target}"})
        size = target.stat().st_size
        if size > self.settings.max_file_bytes:
            return _dump(
                {
                    "ok": False,
                    "error": f"文件过大（{size} bytes），上限 {self.settings.max_file_bytes} bytes",
                    "hint": "请指定更小的文件，或拆分后再审查",
                }
            )
        try:
            content = self.source(target)
        except UnicodeDecodeError:
            return _dump({"ok": False, "error": "无法按 UTF-8 解码，当前只支持文本源码"})
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        language = "python" if target.suffix == ".py" else target.suffix.lstrip(".") or "text"
        preview_lines = content.splitlines()[:40]
        self.memory.upsert_file_fact(
            FileFact(
                path=str(target),
                language=language,
                size_bytes=size,
                sha256=digest,
                summary=f"{len(content.splitlines())} 行; 预览: {' / '.join(preview_lines[:3])[:180]}",
            )
        )
        numbered = "\n".join(f"{index+1:>4}| {line}" for index, line in enumerate(content.splitlines()))
        return _dump(
            {
                "ok": True,
                "path": str(target),
                "language": language,
                "size_bytes": size,
                "sha256": digest,
                "line_count": len(content.splitlines()),
                "content": numbered or content,
            }
        )

    def resolve_path(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        target = ((self.root or Path.cwd()) / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if self.root and not target.is_relative_to(self.root):
            raise ValueError("文件路径超出已加载项目")
        if self.snapshots is not None and str(target) not in self.snapshots:
            raise ValueError("文件不在本次选择的审查范围")
        return target

    def source(self, target: Path) -> str:
        if self.snapshots is not None:
            return self.snapshots[str(target)]
        return target.read_text(encoding="utf-8")

    def parse_python(self, path: str) -> str:
        target = self.resolve_path(path)
        if not target.exists() and self.snapshots is None:
            return _dump({"ok": False, "error": f"文件不存在: {target}"})
        if self.snapshots is None and target.stat().st_size > self.settings.max_file_bytes:
            return _dump({"ok": False, "error": "文件超过大小限制"})
        source = self.source(target)
        result = analyze_python(source, str(target))
        version = hashlib.sha256(source.encode()).hexdigest()
        for issue in result["issues"]:
            finding = self.memory.upsert_finding(Finding(
                finding_id="", path=str(target), severity=issue["severity"], title=issue["message"],
                status="confirmed" if issue["confidence"] == "confirmed" else "open",
                location=str(issue["line"]), evidence=issue["evidence"], suggestion=issue["suggestion"],
                source="tool", code=issue["code"], scope=issue["scope"], fingerprint=issue["fingerprint"],
                confidence=issue["confidence"], end_line=issue["end_line"], version=version,
            ))
            self.touched.add(finding.finding_id)
        return _dump({"ok": True, "path": str(target), **result, "issue_count": len(result["issues"])})

    def record_finding(self, path: str, line: int, title: str, evidence: str, suggestion: str, severity: str) -> str:
        target = self.resolve_path(path)
        if str(target) not in self.memory.file_facts:
            return _dump({"ok": False, "error": "请先读取文件"})
        source = self.source(target)
        lines = source.splitlines()
        if type(line) is not int or not 1 <= line <= len(lines) or severity not in {"高", "中", "低"}:
            return _dump({"ok": False, "error": "行号或严重程度无效"})
        if not evidence.strip() or evidence.strip() not in "\n".join(lines[line - 1:]):
            return _dump({"ok": False, "error": "证据不对应指定位置的源码"})
        if not "\n".join(lines[line - 1:]).lstrip().startswith(evidence.strip()) and evidence.strip() not in lines[line - 1]:
            return _dump({"ok": False, "error": "证据起始行不匹配"})
        fingerprint = hashlib.sha256(f"llm|{title}|{evidence.strip()}|{line}".encode()).hexdigest()[:24]
        # Avoid duplicating an existing static issue for the same location/title.
        existing = next((f for f in self.memory.findings.values() if f.path == str(target) and f.location == str(line) and f.title == title), None)
        if existing and existing.source == "tool":
            return _dump({"ok": True, "finding": existing.__dict__})
        item = self.memory.upsert_finding(Finding(
            finding_id="", path=str(target), severity=severity, title=title, evidence=evidence,
            location=str(line), suggestion=suggestion, source="llm", fingerprint=fingerprint,
            version=hashlib.sha256(source.encode()).hexdigest(), end_line=line + len(evidence.splitlines()) - 1,
        ))
        self.touched.add(item.finding_id)
        return _dump({"ok": True, "finding": item.__dict__})

    def run_snippet(self, code: str, reason: str = "") -> str:
        if not code or not code.strip():
            return _dump({"ok": False, "error": "code 不能为空"})
        validation_error = _validate_snippet(code)
        if validation_error:
            return _dump({"ok": False, "error": validation_error})
        with tempfile.TemporaryDirectory(prefix="cra-snippet-") as tmp:
            script = Path(tmp) / "snippet.py"
            script.write_text(code, encoding="utf-8")
            # 不继承主进程环境，避免 API Key 等秘密进入子进程。
            env = {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONIOENCODING": "utf-8",
            }
            try:
                completed = subprocess.run(
                    [sys.executable, "-I", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=self.settings.snippet_timeout,
                    env=env,
                    cwd=tmp,
                    check=False,
                    close_fds=True,
                    preexec_fn=_limit_snippet_resources if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired:
                return _dump(
                    {
                        "ok": False,
                        "timed_out": True,
                        "reason": reason,
                        "error": f"执行超过 {self.settings.snippet_timeout}s，已中止",
                    }
                )
        return _dump(
            {
                "ok": completed.returncode == 0,
                "reason": reason,
                "exit_code": completed.returncode,
                "stdout": _clip(completed.stdout or "", 2000),
                "stderr": _clip(completed.stderr or "", 2000),
            }
        )

    def remember(
        self,
        kind: str,
        content: str,
        path: str = "",
        severity: str = "中",
        status: str = "open",
        location: str = "",
        finding_id: str = "",
    ) -> str:
        if kind == "preference":
            self.memory.remember_preference(content)
            return _dump({"ok": True, "stored": "preference", "content": content})
        if kind == "note":
            if content not in self.memory.semantic_notes:
                self.memory.semantic_notes.append(content)
            return _dump({"ok": True, "stored": "note", "content": content})
        if kind == "finding":
            if self.root is not None:
                return _dump({"ok": False, "error": "请使用 record_finding；关闭状态只能由复查服务更新"})
            item = self.memory.upsert_finding(
                Finding(
                    finding_id=finding_id,
                    path=path or "unknown",
                    severity=severity,
                    title=content,
                    status=status,
                    location=location,
                    evidence="",
                    suggestion="",
                    source="llm",
                )
            )
            return _dump({"ok": True, "stored": "finding", "finding": item.__dict__})
        return _dump({"ok": False, "error": f"不支持的 kind: {kind}"})

    def recall_memory(self, query: str) -> str:
        needle = (query or "").strip().lower()
        if not needle:
            return _dump({"ok": False, "error": "query 不能为空"})
        files = [
            as_dict
            for as_dict in (item.__dict__ for item in self.memory.file_facts.values())
            if needle in json.dumps(as_dict, ensure_ascii=False).lower()
        ]
        findings = [
            item.__dict__
            for item in self.memory.findings.values()
            if needle in json.dumps(item.__dict__, ensure_ascii=False).lower()
        ]
        episodes = [
            item.__dict__
            for item in self.memory.episodes
            if needle in json.dumps(item.__dict__, ensure_ascii=False).lower()
        ]
        notes = [text for text in self.memory.semantic_notes + self.memory.preferences if needle in text.lower()]
        return _dump(
            {
                "ok": True,
                "query": query,
                "files": files[:8],
                "findings": findings[:12],
                "episodes": episodes[:6],
                "notes": notes[:8],
            }
        )
