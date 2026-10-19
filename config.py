"""Загрузка конфигурации из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    log_chat: str
    save_media: bool
    retention_days: int
    ignore_outgoing: bool


def load_config() -> Config:
    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")

    if not api_id or not api_hash:
        raise SystemExit(
            "Не заданы API_ID / API_HASH. Скопируйте .env.example в .env и заполните "
            "(значения берутся на https://my.telegram.org)."
        )

    try:
        api_id_int = int(api_id)
    except ValueError as exc:  # noqa: PERF203
        raise SystemExit("API_ID должен быть числом.") from exc

    return Config(
        api_id=api_id_int,
        api_hash=api_hash.strip(),
        session_name=os.getenv("SESSION_NAME", "tg_skaner").strip(),
        log_chat=os.getenv("LOG_CHAT", "me").strip(),
        save_media=_as_bool(os.getenv("SAVE_MEDIA"), True),
        retention_days=int(os.getenv("RETENTION_DAYS", "7")),
        ignore_outgoing=_as_bool(os.getenv("IGNORE_OUTGOING"), False),
    )


def resolve_log_chat(value: str):
    """`me` -> Saved Messages, число -> id, иначе — юзернейм как есть."""
    if value == "me":
        return "me"
    try:
        return int(value)
    except ValueError:
        return value
