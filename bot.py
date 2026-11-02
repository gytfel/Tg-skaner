"""Tg-skaner — userbot-архиватор переписок, удалённых и изменённых сообщений.

Захват сообщений работает на вашем аккаунте через api_id/api_hash (Telethon).
Доставка — через отдельного Telegram-бота (токен от @BotFather); если
BOT_TOKEN не задан — в LOG_CHAT (по умолчанию «Избранное»).

Режимы:
  PASSIVE_MODE=true  (тихий, по умолчанию) — никаких сообщений: удаления и
      правки только записываются в архив, а бот присылает лишь zip-файл
      переписок, когда архив достигает EXPORT_LIMIT_MB. После доставки файла
      данные на сервере удаляются.
  PASSIVE_MODE=false — дополнительно каждое удаление/правка сразу приходит
      отдельным сообщением.

Логика:
  1. Каждое входящее/исходящее сообщение кэшируется локально (SQLite) и
     пишется в архив export/archive.jsonl.
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

from archive import Archiver
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
PASSIVE = cfg.passive_mode
store = MessageStore("messages.db")

MEDIA_DIR = "media"
MEDIA_TMP = "media_tmp"  # сюда качаем, и только готовый файл переносим в media/
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(MEDIA_TMP, exist_ok=True)

EXPORT_DIR = "export"
# Единый архив всех копий + автоэкспорт по лимиту. В тихом режиме экспорт —
# единственный канал доставки, поэтому он включён всегда.
archiver = (
    Archiver(EXPORT_DIR, MEDIA_DIR, cfg.export_limit_mb)
    if cfg.export_enabled or PASSIVE
    else None
)

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

STARTUP_TEXT = (
    "✅ <b>Tg-skaner запущен.</b>\n"
    "Отслеживаю удаления и правки сообщений в ваших чатах."
)


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


def _archive(record: dict) -> None:
    """Запись в единый архив (если он включён). Ошибку только логируем."""
    if archiver is None:
        return
    try:
        archiver.append(record)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось записать в архив: %s", exc)


def _row_fields(row) -> dict:
    """Общие поля записи архива из строки кэша messages.db."""
    return {
        "chat_id": row["chat_id"],
        "chat": row["chat_title"],
        "sender_id": row["sender_id"],
        "sender": row["sender_name"],
        "username": row["sender_username"],
        "outgoing": bool(row["outgoing"]),
    }


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
    # Качаем во временную папку и переносим в media/ атомарно — чтобы экспорт
    # никогда не увидел и не удалил недокачанный файл.
    media_path: str | None = None
    if cfg.save_media and msg.media:
        try:
            tmp_prefix = os.path.join(MEDIA_TMP, f"{event.chat_id}_{msg.id}")
            tmp_path = await msg.download_media(file=tmp_prefix)
            if tmp_path:
                final_path = os.path.join(MEDIA_DIR, os.path.basename(tmp_path))
                os.replace(tmp_path, final_path)  # атомарно (та же ФС)
                store.update_media(event.chat_id, msg.id, final_path)
                media_path = final_path
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать медиа: %s", exc)

    # Копия в единый архив (все сообщения, не только удалённые).
    _archive(
        {
            "type": "msg",
            "ts": int(msg.date.timestamp()) if msg.date else int(time.time()),
            "chat_id": event.chat_id,
            "chat": get_display_name(chat) if chat else None,
            "msg_id": msg.id,
            "sender_id": getattr(sender, "id", None),
            "sender": get_display_name(sender) if sender else None,
            "username": getattr(sender, "username", None),
            "outgoing": bool(msg.out),
            "text": msg.message or "",
            "media": media_path,
        }
    )


async def on_edited(event: events.MessageEdited.Event) -> None:
    """Фиксируем правки: «было / стало» — в архив (и сообщением, если не тихий режим)."""
    msg = event.message
    if cfg.ignore_outgoing and msg.out:
        return

    row = store.get_by_chat(event.chat_id, msg.id)
    new_text = msg.message or ""
    old_text = (row["text"] if row else "") or ""

    if row is not None and old_text != new_text:
        edit_date = getattr(msg, "edit_date", None)
        _archive(
            {
                "type": "edit",
                "ts": int(edit_date.timestamp()) if edit_date else int(time.time()),
                **_row_fields(row),
                "msg_id": msg.id,
                "old": old_text,
                "new": new_text,
            }
        )
        if not PASSIVE:
            body = (
                _header(row, "✏️", "Изменённое сообщение")
                + f"\n\n<b>Было:</b>\n{html.escape(old_text) or '<i>(пусто)</i>'}"
                + f"\n\n<b>Стало:</b>\n{html.escape(new_text) or '<i>(пусто)</i>'}"
            )
            await _send(body)

    store.update_text(event.chat_id, msg.id, new_text)


async def on_deleted(event: events.MessageDeleted.Event) -> None:
    """Главное: сообщение удалили — достаём оригинал из кэша и фиксируем."""
    for mid in event.deleted_ids:
        if event.chat_id is not None:
            row = store.get_by_chat(event.chat_id, mid)
        else:
            row = store.get_private(mid)

        if row is None:
            continue  # сообщение не попало в кэш (пришло до запуска бота)
        if cfg.ignore_outgoing and row["outgoing"]:
            continue

        media = row["media_path"]
        media_here = bool(media and os.path.exists(media))
        _archive(
            {
                "type": "del",
                "ts": int(time.time()),
                **_row_fields(row),
                "msg_id": mid,
                "orig_ts": row["date"],
                "text": row["text"] or "",
                "media": media,
                "media_here": media_here,
            }
        )

        if not PASSIVE:
            text = html.escape(row["text"] or "")
            body = _header(row, "🗑", "Удалённое сообщение")
            body += f"\n\n{text}" if text else "\n\n<i>(без текста)</i>"
            if media:
                if media_here:
                    body += "\n\n📎 <i>медиа восстановлено ниже</i>"
                else:
                    body += "\n\n📎 <i>медиа — в экспортном архиве</i>"
            await _send(body, media)
        log.info("Зафиксировано удаление: chat=%s msg=%s", row["chat_id"], mid)


# ─────────────────────────── хендлеры (bot) ─────────────────────────────────

async def on_start(event: events.NewMessage.Event) -> None:
    if PASSIVE:
        text = (
            "✅ Tg-skaner на связи (тихий режим).\n"
            "Сообщений не будет — сюда придёт только zip-файл с перепиской, "
            f"когда архив накопит ~{cfg.export_limit_mb} МБ."
        )
    else:
        text = (
            "✅ Tg-skaner на связи. Сюда будут приходить удалённые и "
            "изменённые сообщения из ваших чатов."
        )
    await event.respond(text)


# ─────────────────────────── фоновая очистка ────────────────────────────────

async def retention_loop() -> None:
    while True:
        try:
            cutoff = int(time.time()) - cfg.retention_days * 86400
            paths = store.cleanup(cutoff)
            # С включённым экспортом медиа удаляет только он — после доставки
            # файла. Иначе вложения пропали бы раньше, чем попали в архив.
            if archiver is None:
                for path in paths:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Очистка кэша не удалась: %s", exc)
        await asyncio.sleep(3600)


def _export_caption(stats: dict, size_mb: float) -> str:
    return (
        f"📦 Архив переписок Tg-skaner (~{size_mb:.1f} МБ)\n"
        f"Сообщений: {stats.get('msg', 0)} · удалено: {stats.get('del', 0)} · "
        f"изменено: {stats.get('edit', 0)} · медиа: {stats.get('media', 0)}\n"
        "Переписки — в папке «переписки». После доставки данные на сервере удалены."
    )


async def _deliver_export(pending) -> None:
    """Отправляет собранный zip и только после успешной доставки чистит диск.
    Исключение пробрасывается — вызывающий код повторит отправку позже."""
    zip_path, part_files, media_files, stats = pending
    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    await _notify_client.send_file(
        _notify_to, zip_path, caption=_export_caption(stats, size_mb)
    )
    archiver.cleanup_after_send(part_files, media_files, zip_path)
    log.info("Экспорт отправлен (~%.1f МБ), данные на сервере очищены.", size_mb)


async def export_loop() -> None:
    """Следит за объёмом архива; при превышении лимита пакует всё в zip,
    отправляет владельцу и чистит локальные копии.

    При сбое отправки НЕ пересобирает zip каждый цикл (иначе шторм упаковки и
    рост .part при оффлайне), а держит готовый zip и повторяет отправку того же
    файла с экспоненциальной паузой.
    """
    if archiver is None:
        return
    pending = None  # (zip_path, part_files, media_files, stats) — собран, не отправлен
    backoff = 60
    while True:
        await asyncio.sleep(backoff)
        try:
            if pending is None:
                if not archiver.over_limit():
                    backoff = 60
                    continue
                pending = await asyncio.to_thread(archiver.build_zip)
                if pending is None:
                    backoff = 60
                    continue

            if _notify_client is None or _notify_to is None:
                backoff = min(backoff * 2, 3600)  # некуда слать — ждём, zip храним
                continue

            try:
                await _deliver_export(pending)
                pending = None
                backoff = 60
            except Exception as exc:  # noqa: BLE001
                # zip сохраняем и повторим его же позже; данные не теряются.
                backoff = min(backoff * 2, 3600)
                log.error(
                    "Не удалось отправить экспорт — повторю через %s c: %s",
                    backoff, exc,
                )
        except Exception as exc:  # noqa: BLE001
            backoff = min(backoff * 2, 3600)
            log.warning("Ошибка экспорта: %s", exc)


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
        if PASSIVE:
            # Тихий режим: ничего не пишем, только проверяем, что бот знает вас.
            try:
                await bot.get_input_entity(_notify_to)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Бот @%s, похоже, ещё не может писать id %s — откройте бота в "
                    "Telegram и нажмите Start (перезапуск не нужен). Детали: %s",
                    bot_me.username, _notify_to, exc,
                )
        else:
            # Стартовое сообщение — громко, чтобы недоступность бота была видна.
            try:
                await bot.send_message(_notify_to, STARTUP_TEXT, parse_mode="html")
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Бот @%s не может написать id %s. Откройте бота в Telegram, "
                    "нажмите Start и перезапустите сервис. Детали: %s",
                    bot_me.username, _notify_to, exc,
                )
    else:
        _notify_client = user
        _notify_to = resolve_log_chat(cfg.log_chat)
        log.info("Доставка в LOG_CHAT: %s", cfg.log_chat)
        if not PASSIVE:
            await _send(STARTUP_TEXT)

    # Хендлеры регистрируем только когда цель доставки уже готова —
    # иначе событие в момент старта ушло бы «в никуда».
    user.add_event_handler(cache_new, events.NewMessage())
    user.add_event_handler(on_edited, events.MessageEdited())
    user.add_event_handler(on_deleted, events.MessageDeleted())
    if bot is not None:
        bot.add_event_handler(on_start, events.NewMessage(pattern=r"^/start"))

    # Подчищаем недокачанные файлы, оставшиеся от прошлого (упавшего) запуска.
    for name in os.listdir(MEDIA_TMP):
        try:
            fp = os.path.join(MEDIA_TMP, name)
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass
    if archiver is not None:
        archiver.clean_stale()

    asyncio.create_task(retention_loop())
    if archiver is not None:
        asyncio.create_task(export_loop())
        log.info(
            "%s Автоэкспорт: файл приходит при ~%s МБ.",
            "Тихий режим: сообщений не будет." if PASSIVE else "Обычный режим.",
            cfg.export_limit_mb,
        )
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
