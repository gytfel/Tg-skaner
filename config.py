"""Загрузка конфигурации из переменных окружения (env/.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Файл настроек лежит в отдельной папке env/ рядом с кодом.
_ROOT = Path(__file__).resolve().parent
_ENV_FILE = _ROOT / "env" / ".env"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
else:
    load_dotenv()  # запасной вариант: .env в текущем каталоге


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_name: str
    bot_token: str | None
    owner_id: int | None
    log_chat: str
    save_media: bool
    retention_days: int
    ignore_outgoing: bool
    export_enabled: bool
    export_limit_mb: int


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

    owner_id_raw = os.getenv("OWNER_ID", "").strip()
    try:
        owner_id = int(owner_id_raw) if owner_id_raw else None
    except ValueError as exc:
        raise SystemExit("OWNER_ID должен быть числом (или пустым).") from exc

    try:
        retention_days = int(os.getenv("RETENTION_DAYS", "7").strip() or "7")
    except ValueError as exc:
        raise SystemExit("RETENTION_DAYS должен быть числом.") from exc

    try:
        export_limit_mb = int(os.getenv("EXPORT_LIMIT_MB", "100").strip() or "100")
    except ValueError as exc:
        raise SystemExit("EXPORT_LIMIT_MB должен быть числом.") from exc

    return Config(
        api_id=api_id_int,
        api_hash=api_hash.strip(),
        session_name=os.getenv("SESSION_NAME", "tg_skaner").strip(),
        bot_token=(os.getenv("BOT_TOKEN", "").strip() or None),
        owner_id=owner_id,
        log_chat=os.getenv("LOG_CHAT", "me").strip(),
        save_media=_as_bool(os.getenv("SAVE_MEDIA"), True),
        retention_days=retention_days,
        ignore_outgoing=_as_bool(os.getenv("IGNORE_OUTGOING"), False),
        export_enabled=_as_bool(os.getenv("EXPORT_ENABLED"), True),
        export_limit_mb=export_limit_mb,
    )


def resolve_log_chat(value: str):
    """`me` -> Saved Messages, число -> id, иначе — юзернейм как есть."""
    if value == "me":
        return "me"
    try:
        return int(value)
    except ValueError:
        return value
