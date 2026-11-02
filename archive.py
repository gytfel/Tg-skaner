"""Единый архив всех копий сообщений + автоэкспорт по лимиту размера.

Каждое проходящее сообщение, а также события «изменено» и «удалено»
дописываются строкой JSON в export/archive.jsonl. Когда суммарный объём
(archive.jsonl + *.part + папка media/) достигает лимита, всё упаковывается в zip:

    переписки/<чат> (<id>).txt  — читаемые переписки, по файлу на чат;
    raw/archive.*.jsonl         — исходные записи (для программ);
    media/                      — вложения.

zip отправляется владельцу, после чего локальные копии удаляются.
messages.db (детекция удалений) не трогается.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import threading
import zipfile
from datetime import datetime

log = logging.getLogger("tg-skaner")

# Запас под лимит Telegram на размер одного файла (~2 ГБ).
MAX_LIMIT_MB = 1900
# Сколько файлов переписок держать открытыми одновременно при сборке.
_MAX_OPEN = 64
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


# ─────────────────────────── форматирование ────────────────────────────────

def _fmt_ts(ts) -> str:
    if not ts:
        return "?"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, TypeError, ValueError):
        return "?"


def _safe_name(name: str) -> str:
    return _BAD_CHARS.sub("_", name).strip(" .")[:60] or "chat"


def _indent(text: str) -> str:
    return text.replace("\n", "\n    ")


def _who(r: dict) -> str:
    if r.get("outgoing"):
        return "Вы"
    name = r.get("sender") or "?"
    if r.get("username"):
        name += f" (@{r['username']})"
    return name


def _media_note(r: dict) -> str:
    media = r.get("media")
    if not media:
        return ""
    base = os.path.basename(media)
    if r.get("media_here", True):
        return f" [медиа: media/{base}]"
    return f" [медиа: {base} — в одном из прошлых архивов]"


def format_record(r: dict) -> str:
    """Одна запись архива -> строка читаемой переписки."""
    kind = r.get("type", "msg")
    ts = _fmt_ts(r.get("ts"))
    who = _who(r)
    num = f" #{r['msg_id']}" if r.get("msg_id") is not None else ""
    if kind == "edit":
        return (
            f"[{ts}] ✏️ {who} изменил(а) сообщение{num}\n"
            f"    было:  {_indent(r.get('old') or '(пусто)')}\n"
            f"    стало: {_indent(r.get('new') or '(пусто)')}"
        )
    body = (_indent(r.get("text") or "") + _media_note(r)).strip()
    if kind == "del":
        return (
            f"[{ts}] 🗑 УДАЛЕНО сообщение{num} от {who} "
            f"(отправлено {_fmt_ts(r.get('orig_ts'))}): {body or '(без текста)'}"
        )
    return f"[{ts}] {who}: {body or '(пусто)'}"


class Archiver:
    def __init__(self, export_dir: str, media_dir: str, limit_mb: int) -> None:
        self.export_dir = export_dir
        self.media_dir = media_dir
        effective = min(max(1, limit_mb), MAX_LIMIT_MB)
        if effective != limit_mb:
            log.warning(
                "EXPORT_LIMIT_MB=%s вне диапазона 1..%s — использую %s.",
                limit_mb, MAX_LIMIT_MB, effective,
            )
        self.limit_bytes = effective * 1024 * 1024
        self.archive_path = os.path.join(export_dir, "archive.jsonl")
        # threading.Lock: append() идёт из event-loop, build_zip() — из рабочего
        # потока (to_thread); обычный asyncio.Lock их бы не синхронизировал.
        self._lock = threading.Lock()
        os.makedirs(export_dir, exist_ok=True)

    # ─────────────────────────── запись ────────────────────────────────────
    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self.archive_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ─────────────────────────── измерение ─────────────────────────────────
    def _dir_size(self, path: str) -> int:
        total = 0
        if os.path.isdir(path):
            for name in os.listdir(path):
                fp = os.path.join(path, name)
                try:
                    if os.path.isfile(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def _part_files(self) -> list[str]:
        return sorted(glob.glob(os.path.join(self.export_dir, "archive.*.part")))

    def total_bytes(self) -> int:
        size = self._dir_size(self.media_dir)
        for p in [self.archive_path, *self._part_files()]:
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        return size

    def over_limit(self) -> bool:
        return self.total_bytes() >= self.limit_bytes

    # ─────────────────────────── экспорт ───────────────────────────────────
    def _list_media(self) -> list[str]:
        if not os.path.isdir(self.media_dir):
            return []
        return [
            os.path.join(self.media_dir, n)
            for n in os.listdir(self.media_dir)
            if os.path.isfile(os.path.join(self.media_dir, n))
        ]

    def render_chats(self, part_files: list[str], out_dir: str) -> dict:
        """Раскладывает записи по файлам-перепискам (по чату), потоково —
        без загрузки всего архива в память. Возвращает счётчики записей."""
        stats = {"msg": 0, "edit": 0, "del": 0}
        names: dict[str, str] = {}
        handles: dict = {}
        try:
            for p in part_files:
                try:
                    f = open(p, encoding="utf-8", errors="replace")
                except OSError:
                    continue
                with f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(r, dict):
                            continue
                        key = str(r.get("chat_id"))
                        fh = handles.get(key)
                        if fh is None:
                            if len(handles) >= _MAX_OPEN:
                                for h in handles.values():
                                    h.close()
                                handles.clear()
                            if key not in names:
                                names[key] = f"{_safe_name(r.get('chat') or 'chat')} ({key}).txt"
                            path = os.path.join(out_dir, names[key])
                            is_new = not os.path.exists(path)
                            fh = open(path, "a", encoding="utf-8")
                            if is_new:
                                fh.write(f"══ Переписка: {r.get('chat') or '—'} (id {key}) ══\n\n")
                            handles[key] = fh
                        fh.write(format_record(r) + "\n")
                        kind = r.get("type", "msg")
                        if kind in stats:
                            stats[kind] += 1
        finally:
            for h in handles.values():
                h.close()
        return stats

    def build_zip(self):
        """Готовит zip к отправке (блокирующая операция — вызывать в to_thread).

        Возвращает (zip_path, written_parts, written_media, stats) либо None,
        если экспортировать нечего. Пишем во временный файл и переименовываем —
        чтобы при ошибке (ENOSPC и т.п.) не оставить битый .zip. Удаляются
        потом только те файлы, что реально попали в архив.
        """
        with self._lock:
            if os.path.exists(self.archive_path) and os.path.getsize(self.archive_path) > 0:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                os.replace(
                    self.archive_path,
                    os.path.join(self.export_dir, f"archive.{stamp}.part"),
                )

        part_files = self._part_files()
        media_files = self._list_media()
        if not part_files and not media_files:
            return None

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = os.path.join(self.export_dir, f"tg-skaner-export-{ts}.zip")
        tmp_path = zip_path + ".tmp"
        render_dir = os.path.join(self.export_dir, f".render-{ts}")
        written_parts: list[str] = []
        written_media: list[str] = []
        try:
            os.makedirs(render_dir, exist_ok=True)
            stats = self.render_chats(part_files, render_dir)
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for name in sorted(os.listdir(render_dir)):
                    zf.write(os.path.join(render_dir, name), arcname=f"переписки/{name}")
                for p in part_files:
                    try:
                        raw = os.path.basename(p)[: -len(".part")] + ".jsonl"
                        zf.write(p, arcname=f"raw/{raw}")
                        written_parts.append(p)
                    except OSError:
                        pass
                for m in media_files:
                    try:
                        # медиа уже сжато — не тратим CPU на DEFLATE.
                        zf.write(
                            m,
                            arcname=f"media/{os.path.basename(m)}",
                            compress_type=zipfile.ZIP_STORED,
                        )
                        written_media.append(m)
                    except OSError:
                        pass  # файл исчез во время упаковки — просто пропускаем
            os.replace(tmp_path, zip_path)
        except BaseException:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(render_dir, ignore_errors=True)
        stats["media"] = len(written_media)
        return zip_path, written_parts, written_media, stats

    def cleanup_after_send(
        self, part_files: list[str], media_files: list[str], zip_path: str
    ) -> None:
        """Удаляет локальные копии ПОСЛЕ успешной отправки (освобождает диск).

        Удаляются только файлы, реально попавшие в zip; записи/медиа, добавленные
        во время экспорта (и не вошедшие в архив), остаются.
        """
        for p in [*part_files, *media_files, zip_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def clean_stale(self) -> None:
        """Убирает хвосты прошлого (упавшего) запуска: неотправленные/недособранные
        zip и временные папки. Данные не теряются — они остались в *.part и
        media/ и попадут в следующий экспорт."""
        for pattern in ("tg-skaner-export-*.zip", "tg-skaner-export-*.zip.tmp"):
            for p in glob.glob(os.path.join(self.export_dir, pattern)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        for d in glob.glob(os.path.join(self.export_dir, ".render-*")):
            shutil.rmtree(d, ignore_errors=True)
