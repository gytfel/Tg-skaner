"""Tg-skaner — userbot-архиватор удалённых и изменённых сообщений.

Захват сообщений работает на вашем аккаунте через api_id/api_hash (Telethon).
Доставка отчётов — через отдельного Telegram-бота (токен от @BotFather):
удаления и правки прилетают вам в личку с ботом, а не в «Избранное».
Если BOT_TOKEN не задан — откат к доставке в LOG_CHAT (по умолчанию «Избранное»).

Логика:
  1. Каждое входящее/исходящее сообщение кэшируется локально (SQLite).
  2. При удалении Telegram присылает только id — оригинал достаётся из кэша.
  3. Правки фиксируются как «было / стало».

Только для вашего аккаунта и ваших переписок.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
import time
from datetime import datetime

from telethon import TelegramClient, events
from telethon.utils import get_display_name

from config import load_config, resolve_log_chat
from db import MessageStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-skaner")

# Код выхода, который говорит systemd НЕ перезапускать сервис (нет сессии).
EXIT_NO_SESSION = 69

cfg = load_config()
store = MessageStore("messages.db")

MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

# Клиент-пользователь: ловит сообщения в ваших чатах.
user = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
# Клиент-бот: доставляет отчёты вам в личку (если задан BOT_TOKEN).
bot = (
    TelegramClient(cfg.session_name + "_bot", cfg.api_id, cfg.api_hash)
    if cfg.bot_token
    else None
)

# Куда и чем слать отчёты — заполняется в main() до регистрации хендлеров.
_notify_client: TelegramClient | None = None
_notify_to = None


# ─────────────────────────── вспомогательное ────────────────────────────────

def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _header(row, emoji: str, label: str) -> str:
    sender = html.escape(row["sender_name"] or "Неизвестно")
    uname = f" @{html.escape(row['sender_username'])}" if row["sender_username"] else ""
    chat = html.escape(row["chat_title"] or "—")
    direction = "исходящее (удалили вы)" if row["outgoing"] else "входящее"
    return (
        f"{emoji} <b>{label}</b>\n"
        f"👤 Отправитель: {sender}{uname} (id: <code>{row['sender_id']}</code>)\n"
        f"💬 Чат: {chat}\n"
        f"↕️ Направление: {direction}\n"
        f"🕐 Отправлено: {_fmt_ts(row['date'])}\n"
        f"⚡ Зафиксировано: {_fmt_ts(int(time.time()))}"
    )


async def _send(text: str, media_path: str | None = None) -> None:
    """Доставка отчёта. Best-effort: ошибку логируем, но не роняем процесс."""
    if _notify_client is None or _notify_to is None:
        return
    try:
        await _notify_client.send_message(
            _notify_to, text, parse_mode="html", link_preview=False
        )
        if media_path and os.path.exists(media_path):
            await _notify_client.send_file(_notify_to, media_path)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Не удалось доставить отчёт. Если используете бота — откройте его в "
            "Telegram и нажмите Start. Детали: %s",
            exc,
        )


# ─────────────────────────── хендлеры (user) ────────────────────────────────

async def cache_new(event: events.NewMessage.Event) -> None:
    """Сохраняем каждое проходящее сообщение в кэш."""
    msg = event.message
    if cfg.ignore_outgoing and msg.out:
        return

    try:
        sender = await event.get_sender()
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        sender = chat = None

    # Сначала фиксируем текст и метаданные — они не должны зависеть от того,
    # успеет ли докачаться медиа до возможного удаления.
    store.add(
        chat_id=event.chat_id,
        message_id=msg.id,
        is_channel=1 if event.is_channel else 0,
        sender_id=getattr(sender, "id", None),
        sender_name=get_display_name(sender) if sender else None,
        sender_username=getattr(sender, "username", None),
        chat_title=get_display_name(chat) if chat else None,
        text=msg.message or "",
        media_path=None,
        outgoing=1 if msg.out else 0,
        date=int(msg.date.timestamp()) if msg.date else None,
    )

    # Медиа качаем уже после сохранения строки; путь дописываем по готовности.
    if cfg.save_media and msg.media:
        try:
            prefix = os.path.join(MEDIA_DIR, f"{event.chat_id}_{msg.id}")
            path = await msg.download_media(file=prefix)
            if path:
                store.update_media(event.chat_id, msg.id, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать медиа: %s", exc)


async def on_edited(event: events.MessageEdited.Event) -> None:
    """Фиксируем правки: показываем «было / стало» и обновляем кэш."""
    msg = event.message
    if cfg.ignore_outgoing and msg.out:
        return

    row = store.get_by_chat(event.chat_id, msg.id)
    new_text = msg.message or ""
    old_text = (row["text"] if row else "") or ""

    if row is not None and old_text != new_text:
        body = (
            _header(row, "✏️", "Изменённое сообщение")
            + f"\n\n<b>Было:</b>\n{html.escape(old_text) or '<i>(пусто)</i>'}"
            + f"\n\n<b>Стало:</b>\n{html.escape(new_text) or '<i>(пусто)</i>'}"
        )
        await _send(body)

    store.update_text(event.chat_id, msg.id, new_text)


async def on_deleted(event: events.MessageDeleted.Event) -> None:
    """Главное: сообщение удалили — достаём оригинал из кэша и пересылаем."""
    for mid in event.deleted_ids:
        if event.chat_id is not None:
            row = store.get_by_chat(event.chat_id, mid)
        else:
            row = store.get_private(mid)

        if row is None:
            continue  # сообщение не попало в кэш (пришло до запуска бота)
        if cfg.ignore_outgoing and row["outgoing"]:
            continue

        text = html.escape(row["text"] or "")
        body = _header(row, "🗑", "Удалённое сообщение")
        body += f"\n\n{text}" if text else "\n\n<i>(без текста)</i>"
        if row["media_path"]:
            body += "\n\n📎 <i>медиа восстановлено ниже</i>"
        await _send(body, row["media_path"])
        log.info("Зафиксировано удаление: chat=%s msg=%s", row["chat_id"], mid)


# ─────────────────────────── хендлеры (bot) ─────────────────────────────────

async def on_start(event: events.NewMessage.Event) -> None:
    await event.respond(
        "✅ Tg-skaner на связи. Сюда будут приходить удалённые и "
        "изменённые сообщения из ваших чатов."
    )


# ─────────────────────────── фоновая очистка ────────────────────────────────

async def retention_loop() -> None:
    while True:
        try:
            cutoff = int(time.time()) - cfg.retention_days * 86400
            for path in store.cleanup(cutoff):
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Очистка кэша не удалась: %s", exc)
        await asyncio.sleep(3600)


# ───────────────────────────────── запуск ───────────────────────────────────

async def _start_user() -> None:
    """Подключаем user-клиент. Без валидной сессии: в терминале — интерактивный
    вход, под systemd (нет TTY) — внятная ошибка и выход без краш-цикла."""
    await user.connect()
    if not await user.is_user_authorized():
        if sys.stdin and sys.stdin.isatty():
            await user.start()  # интерактивный вход в реальном терминале
        else:
            log.error(
                "Нет валидной сессии %s.session. Выполните один раз: python login.py",
                cfg.session_name,
            )
            await user.disconnect()
            raise SystemExit(EXIT_NO_SESSION)


async def main() -> None:
    global _notify_client, _notify_to

    await _start_user()
    me = await user.get_me()
    log.info("Вход как пользователь: %s (id %s)", get_display_name(me), me.id)

    coros = [user.run_until_disconnected()]

    if bot is not None:
        await bot.start(bot_token=cfg.bot_token)
        bot_me = await bot.get_me()
        _notify_client = bot
        _notify_to = cfg.owner_id or me.id
        coros.append(bot.run_until_disconnected())
        log.info("Доставка через бота @%s -> id %s", bot_me.username, _notify_to)
        # Стартовое сообщение — громко, чтобы недоступность бота была видна.
        try:
            await bot.send_message(
                _notify_to,
                "✅ <b>Tg-skaner запущен.</b>\n"
                "Отслеживаю удаления и правки сообщений в ваших чатах.",
                parse_mode="html",
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Бот @%s не может написать id %s. Откройте бота в Telegram, "
                "нажмите Start и перезапустите сервис. Детали: %s",
                bot_me.username,
                _notify_to,
                exc,
            )
    else:
        _notify_client = user
        _notify_to = resolve_log_chat(cfg.log_chat)
        log.info("Доставка в LOG_CHAT: %s", cfg.log_chat)
        await _send(
            "✅ <b>Tg-skaner запущен.</b>\n"
            "Отслеживаю удаления и правки сообщений в ваших чатах."
        )

    # Хендлеры регистрируем только когда цель доставки уже готова —
    # иначе событие в момент старта ушло бы «в никуда».
    user.add_event_handler(cache_new, events.NewMessage())
    user.add_event_handler(on_edited, events.MessageEdited())
    user.add_event_handler(on_deleted, events.MessageDeleted())
    if bot is not None:
        bot.add_event_handler(on_start, events.NewMessage(pattern=r"^/start"))

    asyncio.create_task(retention_loop())
    log.info("Слушаю события. Ctrl+C для остановки.")

    # Если любой из клиентов отвалится — выходим с ненулевым кодом, чтобы
    # systemd перезапустил полностью рабочую пару, а не «половину».
    tasks = [asyncio.ensure_future(c) for c in coros]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
    finally:
        store.close()
