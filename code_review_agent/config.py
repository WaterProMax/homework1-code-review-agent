from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """环境配置不完整或非法。"""


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，收到: {raw!r}") from exc


def _as_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，收到: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    max_steps: int = 8
    request_timeout: float = 45.0
    max_retries: int = 3
    working_memory_token_budget: int = 7000
    recent_turns_keep: int = 6
    max_file_bytes: int = 80_000
    snippet_timeout: float = 2.0
    session_dir: Path = Path(".code_review_agent/sessions")
    default_session: str = "default"


def load_settings(require_api_key: bool = True) -> Settings:
    load_dotenv()
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if require_api_key and not api_key:
        raise ConfigError(
            "缺少 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填写密钥。"
        )
    return Settings(
        api_key=api_key,
        base_url=(os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
        model=(os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip(),
        max_steps=max(2, _as_int("CODE_REVIEW_MAX_STEPS", 8)),
        request_timeout=_as_float("CODE_REVIEW_TIMEOUT", 45.0),
        max_retries=max(1, _as_int("CODE_REVIEW_MAX_RETRIES", 3)),
        working_memory_token_budget=max(1500, _as_int("CODE_REVIEW_MEMORY_BUDGET", 7000)),
        recent_turns_keep=max(2, _as_int("CODE_REVIEW_RECENT_TURNS", 6)),
        max_file_bytes=max(1024, _as_int("CODE_REVIEW_MAX_FILE_BYTES", 80_000)),
        snippet_timeout=_as_float("CODE_REVIEW_SNIPPET_TIMEOUT", 2.0),
        session_dir=Path(os.getenv("CODE_REVIEW_SESSION_DIR") or ".code_review_agent/sessions"),
        default_session=(os.getenv("CODE_REVIEW_SESSION") or "default").strip() or "default",
    )
