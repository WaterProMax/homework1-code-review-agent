from __future__ import annotations

import argparse
import sys
from pathlib import Path

from code_review_agent.agent import CodeReviewAgent
from code_review_agent.config import ConfigError, load_settings
from code_review_agent.memory import MemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-agent",
        description="Python 代码审查 Agent（ReAct + 分层记忆）",
    )
    parser.add_argument("--session", help="会话 ID，默认 default。相同 ID 会复用磁盘记忆。")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地代码审查工作台")
    serve.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    serve.add_argument("--port", type=int, default=8000)

    review = sub.add_parser("review", help="审查 Python 文件或目录；单文件可用同一 --session 追问")
    review.add_argument("path", help="待审查文件路径")
    review.add_argument("instruction", nargs="*", help="额外审查要求，例如：只看高危")

    chat = sub.add_parser("chat", help="多轮对话，自动加载会话记忆")
    chat.add_argument("text", nargs="*", help="可选首条消息；省略则进入交互")

    sub.add_parser("memory", help="打印当前会话的分层记忆摘要")
    reset = sub.add_parser("reset", help="清空指定会话记忆")
    reset.add_argument("--yes", action="store_true", help="确认删除")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            import uvicorn
            from code_review_agent.api import create_app
        except ImportError:
            print('请先安装 Web 依赖：pip install -e ".[web]"', file=sys.stderr)
            return 2
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    try:
        settings = load_settings(require_api_key=args.command not in {"memory", "reset"})
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    session_id = args.session or settings.default_session
    memory = MemoryStore.load(
        session_id=session_id,
        session_dir=settings.session_dir,
        token_budget=settings.working_memory_token_budget,
        recent_turns_keep=settings.recent_turns_keep,
    )

    if args.command == "memory":
        print(f"会话: {memory.session_id}")
        print(f"文件: {memory.path}")
        print(memory.overlay_text())
        return 0

    if args.command == "reset":
        if not args.yes:
            print("未删除。若确认清空记忆，请加上 --yes。")
            return 1
        if memory.path.exists():
            memory.path.unlink()
            print(f"已删除会话记忆: {memory.path}")
        else:
            print("该会话还没有记忆文件。")
        return 0

    if not settings.api_key:
        print("缺少 DEEPSEEK_API_KEY。", file=sys.stderr)
        return 2

    agent = CodeReviewAgent(settings, memory)

    if args.command == "review" and Path(args.path).is_dir():
        import time
        from code_review_agent.review_service import ReviewService
        service = ReviewService(settings)
        try:
            project = service.load_project(args.path)
            paths = [f["path"] for f in project["files"] if f["available"]]
            session = service.create_session(project["id"])
            job = service.submit(session["id"], "review", paths, " ".join(args.instruction))
            while job["status"] not in {"completed", "partial", "failed", "interrupted"}:
                time.sleep(0.2)
                job = service.job(job["id"])
            if job["result"] and job["result"].get("run_id"):
                print(service.report(job["result"]["run_id"]))
                return 0
            print(job["error"], file=sys.stderr)
            return 1
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            service.close()

    if args.command == "review":
        path = args.path
        extra = " ".join(args.instruction).strip()
        prompt = f"请审查文件 `{path}`。"
        if extra:
            prompt += f" 额外要求：{extra}"
            memory.remember_preference(extra)
        result = agent.run(prompt)
        _print_result(result.reply, result.tools_used, memory.path)
        return 0

    initial = " ".join(getattr(args, "text", []) or []).strip()
    if args.command in {None, "chat"} and initial:
        result = agent.run(initial)
        _print_result(result.reply, result.tools_used, memory.path)
        return 0

    print(f"代码审查 Agent 已启动，会话 `{memory.session_id}`。输入 :quit 退出，:memory 查看记忆。")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0
        if not user_input:
            continue
        if user_input in {":quit", ":exit", "quit", "exit"}:
            print("再见。")
            return 0
        if user_input in {":memory", "memory"}:
            print(memory.overlay_text())
            continue
        result = agent.run(user_input)
        _print_result(result.reply, result.tools_used, memory.path)


def _print_result(reply: str, tools_used: list[str], memory_path: Path) -> None:
    print()
    print(reply.rstrip())
    print()
    if tools_used:
        print(f"[tools] {' -> '.join(tools_used)}")
    print(f"[memory] {memory_path}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
