"""Tg-skaner — userbot-архиватор удалённых и изменённых сообщений.

Работает на вашем собственном аккаунте через api_id/api_hash (Telethon).
Логика:
  1. Каждое входящее/исходящее сообщение сохраняется в локальный кэш (SQLite).
  2. Когда Telegram сообщает об удалении — оригинал достаётся из кэша и
     пересылается в LOG_CHAT (по умолчанию — ваше «Избранное»).
  3. Правки сообщений фиксируются с показом «было / стало».

Только для вашего аккаунта и ваших переписок.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
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

cfg = load_config()
store = MessageStore("messages.db")

MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

client = TelegramClient(cfg.session_name, cfg.api_id, cfg.api_hash)
LOG_TARGET = resolve_log_chat(cfg.log_chat)


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
    try:
        await client.send_message(LOG_TARGET, text, parse_mode="html", link_preview=False)
        if media_path and os.path.exists(media_path):
            await client.send_file(LOG_TARGET, media_path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось отправить отчёт в LOG_CHAT: %s", exc)


# ─────────────────────────────── хендлеры ───────────────────────────────────

@client.on(events.NewMessage)
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

    media_path: str | None = None
    if cfg.save_media and msg.media:
        try:
            prefix = os.path.join(MEDIA_DIR, f"{event.chat_id}_{msg.id}")
            media_path = await msg.download_media(file=prefix)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать медиа: %s", exc)

    store.add(
        chat_id=event.chat_id,
        message_id=msg.id,
        is_channel=1 if event.is_channel else 0,
        sender_id=getattr(sender, "id", None),
        sender_name=get_display_name(sender) if sender else None,
        sender_username=getattr(sender, "username", None),
        chat_title=get_display_name(chat) if chat else None,
        text=msg.message or "",
        media_path=media_path,
        outgoing=1 if msg.out else 0,
        date=int(msg.date.timestamp()) if msg.date else None,
    )


@client.on(events.MessageEdited)
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


@client.on(events.MessageDeleted)
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

async def main() -> None:
    await client.start()  # при первом запуске спросит номер, код и (если есть) 2FA-пароль
    me = await client.get_me()
    log.info("Вход выполнен: %s (id %s)", get_display_name(me), me.id)
    await _send(
        "✅ <b>Tg-skaner запущен.</b>\n"
        "Отслеживаю удаления и правки сообщений в ваших чатах."
    )
    asyncio.create_task(retention_loop())
    log.info("Слушаю события. Ctrl+C для остановки.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем.")
    finally:
        store.close()
