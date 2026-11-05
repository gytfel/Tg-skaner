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
import errno
import glob
import html
import logging
import os
import shutil
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

MB = 1024 * 1024
# Ниже этого запаса свободного места включается аварийный режим: самые старые
# медиа удаляются, чтобы бот не встал из-за полного диска.
EMERGENCY_FREE = 150 * MB
# Сторожевой таймер: как часто проверять связь с Telegram и сколько подряд
# неудач терпеть, прежде чем перезапуститься (systemd поднимет процесс заново).
WATCHDOG_EVERY = 300
WATCHDOG_MAX_FAILS = 3
# Если событий нет так долго — считаем, что поток обновлений «залип», и
# перезапускаемся (Telethon иногда молча перестаёт получать апдейты).
STALL_SECONDS = 4 * 3600

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
# Чаты, которые НЕ архивируем: чат с нашим ботом (или LOG_CHAT). Иначе каждый
# присланный zip скачивался бы обратно в media/ и вкладывался в следующий
# архив — архив рос бы сам из себя, а диск забивался.
_own_chats: set[int] = set()

# Время последнего события (для сторожевого таймера и /status).
_last_event = time.monotonic()
_started = time.monotonic()
# Когда последний раз писали в лог о пропуске медиа (чтобы не спамить).
_last_skip_log = 0.0
_last_export_ok: float | None = None

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


def _free_bytes() -> int:
    try:
        return shutil.disk_usage(".").free
    except OSError:
        return 0


def _media_skip_reason(msg) -> str | None:
    """Почему медиа НЕ качаем (или None — качаем). Главная защита диска."""
    size = getattr(msg.file, "size", None) if msg.file else None
    if cfg.max_media_mb and size and size > cfg.max_media_mb * MB:
        return f"{size / MB:.0f} МБ > лимита MAX_MEDIA_MB={cfg.max_media_mb}"
    need = cfg.min_free_mb * MB + (size or 0)
    if _free_bytes() < need:
        return f"мало места на диске (< {cfg.min_free_mb} МБ свободно)"
    return None


def _log_skip(reason: str) -> None:
    global _last_skip_log
    now = time.monotonic()
    if now - _last_skip_log > 600:  # не чаще раза в 10 минут
        _last_skip_log = now
        log.warning("Медиа пропущено: %s", reason)


def _remove_partial(prefix: str) -> None:
    """Удаляет недокачанный файл после ошибки загрузки. Telethon дописывает
    к имени расширение («<prefix>.jpg»), поэтому ищем ровно prefix и prefix.*
    (а не prefix* — иначе задели бы «<chat>_45…» при удалении «<chat>_4»)."""
    for fp in [prefix, *glob.glob(glob.escape(prefix) + ".*")]:
        if not os.path.isfile(fp):
            continue
        try:
            os.remove(fp)
        except OSError:
            pass


# ─────────────────────────── хендлеры (user) ────────────────────────────────

async def cache_new(event: events.NewMessage.Event) -> None:
    """Сохраняем каждое проходящее сообщение в кэш."""
    global _last_event
    if event.chat_id in _own_chats:
        return
    _last_event = time.monotonic()
    msg = event.message
    if cfg.ignore_outgoing and msg.out:
        return

    try:
        sender = await event.get_sender()
        chat = await event.get_chat()
    except Exception:  # noqa: BLE001
        sender = chat = None

    # Каналы-ленты (не группы) — это не переписки, а основной источник
    # гигабайтов медиа. По умолчанию их пропускаем (ARCHIVE_CHANNELS=false).
    if not cfg.archive_channels and getattr(chat, "broadcast", False):
        return

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
    media_skipped: str | None = None
    if cfg.save_media and msg.media:
        media_skipped = _media_skip_reason(msg)
        if media_skipped:
            _log_skip(media_skipped)
        else:
            tmp_prefix = os.path.join(MEDIA_TMP, f"{event.chat_id}_{msg.id}")
            try:
                tmp_path = await msg.download_media(file=tmp_prefix)
                if tmp_path:
                    final_path = os.path.join(MEDIA_DIR, os.path.basename(tmp_path))
                    os.replace(tmp_path, final_path)  # атомарно (та же ФС)
                    store.update_media(event.chat_id, msg.id, final_path)
                    media_path = final_path
            except Exception as exc:  # noqa: BLE001
                _remove_partial(tmp_prefix)  # не оставляем мусор на диске
                media_skipped = "ошибка загрузки"
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
            "media_skipped": media_skipped,
        }
    )


async def on_edited(event: events.MessageEdited.Event) -> None:
    """Фиксируем правки: «было / стало» — в архив (и сообщением, если не тихий режим)."""
    global _last_event
    if event.chat_id in _own_chats:
        return
    _last_event = time.monotonic()
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
    global _last_event
    if event.chat_id is not None and event.chat_id in _own_chats:
        return
    _last_event = time.monotonic()
    for mid in event.deleted_ids:
        if event.chat_id is not None:
            row = store.get_by_chat(event.chat_id, mid)
        else:
            row = store.get_private(mid)

        if row is None:
            continue  # сообщение не попало в кэш (пришло до запуска бота)
        if row["chat_id"] in _own_chats:
            continue
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


def _ago(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds} с"
    if seconds < 7200:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"


async def on_status(event: events.NewMessage.Event) -> None:
    """/status — короткая сводка о здоровье бота (только владельцу)."""
    if event.sender_id != _notify_to:
        return
    now = time.monotonic()
    data = archiver.total_bytes() / MB if archiver else 0.0
    last_export = _ago(now - _last_export_ok) + " назад" if _last_export_ok else "ещё не было"
    text = (
        "📊 Tg-skaner\n"
        f"Работает: {_ago(now - _started)}\n"
        f"Последнее событие: {_ago(now - _last_event)} назад\n"
        f"Свободно на диске: {_free_bytes() / MB:.0f} МБ\n"
        f"Ждёт выгрузки: {data:.1f} МБ (порог {cfg.export_limit_mb} МБ)\n"
        f"Последний экспорт: {last_export}"
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
    global _last_export_ok
    zip_path, part_files, media_files, stats = pending
    size_mb = os.path.getsize(zip_path) / MB
    # Таймаут, чтобы зависшая выгрузка не держала экспорт (и диск) вечно.
    timeout = 600 + size_mb * 30
    await asyncio.wait_for(
        _notify_client.send_file(
            _notify_to, zip_path, caption=_export_caption(stats, size_mb)
        ),
        timeout=timeout,
    )
    archiver.cleanup_after_send(part_files, media_files, zip_path)
    _last_export_ok = time.monotonic()
    log.info("Экспорт отправлен (~%.1f МБ), данные на сервере очищены.", size_mb)


def _emergency_free_disk() -> None:
    """Если диск почти полон — удаляем самые старые медиа. Лучше потерять
    часть вложений, чем получить «мёртвого» бота, который ничего не пишет."""
    free = archiver.free_bytes()
    if free >= EMERGENCY_FREE:
        return
    count, freed = archiver.drop_oldest_media(2 * EMERGENCY_FREE - free)
    log.error(
        "Диск почти заполнен (свободно %.0f МБ): удалено %s старых медиа "
        "(%.0f МБ), чтобы бот продолжил работу.",
        free / MB, count, freed / MB,
    )


async def export_loop() -> None:
    """Следит за объёмом архива; при превышении лимита (или когда на диске
    мало места) пакует данные в zip, отправляет владельцу и чистит копии.

    Медиа уходит порциями не больше лимита, поэтому сборка zip не требует
    много свободного места, а накопившийся объём выгружается несколькими
    файлами подряд. При сбое отправки держим готовый zip и повторяем его же
    с экспоненциальной паузой (а скачивание нового медиа само остановится,
    если места станет мало — см. MIN_FREE_MB).
    """
    if archiver is None:
        return
    pending = None  # (zip_path, part_files, media_files, stats) — собран, не отправлен
    delay = 60
    backoff = 60
    while True:
        await asyncio.sleep(delay)
        delay = 60
        try:
            _emergency_free_disk()
            if pending is None:
                low_disk = archiver.free_bytes() < cfg.min_free_mb * MB
                # При нехватке места выгружаем раньше лимита, но не по крохам.
                early = low_disk and archiver.total_bytes() >= min(
                    archiver.limit_bytes, 20 * MB
                )
                if not (archiver.over_limit() or early):
                    continue
                # Порция не больше лимита и не больше, чем реально влезет на диск.
                budget = min(
                    archiver.limit_bytes,
                    max(MB, archiver.free_bytes() - EMERGENCY_FREE),
                )
                try:
                    pending = await asyncio.to_thread(archiver.build_zip, budget)
                except OSError as exc:
                    if exc.errno == errno.ENOSPC:
                        log.error("Нет места для сборки zip — освобождаю диск.")
                        archiver.drop_oldest_media(archiver.limit_bytes)
                        delay = 5
                        continue
                    raise
                if pending is None:
                    continue

            if _notify_client is None or _notify_to is None:
                backoff = min(backoff * 2, 3600)  # некуда слать — ждём, zip храним
                delay = backoff
                continue

            try:
                await _deliver_export(pending)
                pending = None
                backoff = 60
                delay = 5  # сразу проверяем, не осталось ли ещё порций
            except Exception as exc:  # noqa: BLE001
                # zip сохраняем и повторим его же позже; данные не теряются.
                backoff = min(backoff * 2, 3600)
                delay = backoff
                log.error(
                    "Не удалось отправить экспорт — повторю через %s c: %r",
                    backoff, exc,
                )
        except Exception as exc:  # noqa: BLE001
            backoff = min(backoff * 2, 3600)
            delay = backoff
            log.warning("Ошибка экспорта: %r", exc)


async def watchdog_loop() -> None:
    """Проверяет, что связь с Telegram жива и события идут. Если нет —
    завершается, и main() выходит с ошибкой, чтобы systemd перезапустил бота
    (раньше процесс мог «висеть» живым, но ничего не делать)."""
    fails = 0
    while True:
        await asyncio.sleep(WATCHDOG_EVERY)
        try:
            await asyncio.wait_for(user.get_me(), timeout=60)
            if bot is not None:
                await asyncio.wait_for(bot.get_me(), timeout=60)
            fails = 0
        except Exception as exc:  # noqa: BLE001
            fails += 1
            log.warning("Нет связи с Telegram (%s/%s): %r", fails, WATCHDOG_MAX_FAILS, exc)
            if fails >= WATCHDOG_MAX_FAILS:
                log.error("Связь не восстановилась — перезапуск.")
                return
        if time.monotonic() - _last_event > STALL_SECONDS:
            log.error(
                "Событий нет уже %s — похоже, поток обновлений завис. Перезапуск.",
                _ago(time.monotonic() - _last_event),
            )
            return


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
        _own_chats.add(bot_me.id)  # чат «вы ↔ бот» не архивируем
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
        try:
            _own_chats.add(await user.get_peer_id(_notify_to))
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось определить LOG_CHAT %s: %s", cfg.log_chat, exc)
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
        bot.add_event_handler(on_status, events.NewMessage(pattern=r"^/status"))

    # Подчищаем недокачанные файлы, оставшиеся от прошлого (упавшего) запуска.
    for name in os.listdir(MEDIA_TMP):
        try:
            fp = os.path.join(MEDIA_TMP, name)
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass
    # Копии из чата с ботом, скачанные старой версией (в т.ч. наши же zip):
    # файлы медиа называются «<chat_id>_<msg_id>.<ext>».
    removed = 0
    for cid in _own_chats:
        for fp in glob.glob(os.path.join(MEDIA_DIR, glob.escape(f"{cid}_") + "*")):
            try:
                os.remove(fp)
                removed += 1
            except OSError:
                pass
    if removed:
        log.info("Удалено %s файлов из чата с ботом (копии прошлых архивов).", removed)
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

    # Если любой из клиентов отвалится или сторожевой таймер увидит зависание —
    # выходим с ненулевым кодом, чтобы systemd перезапустил бота целиком.
    coros.append(watchdog_loop())
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
