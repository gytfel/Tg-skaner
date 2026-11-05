"""Единый архив всех копий сообщений + автоэкспорт по лимиту размера.

Каждое проходящее сообщение, а также события «изменено» и «удалено»
дописываются строкой JSON в export/archive.jsonl. Когда суммарный объём
(archive.jsonl + *.part + папка media/) достигает лимита, всё упаковывается в zip:

    index.html                  — оглавление: все чаты с типом, id, счётчиками;
    чаты/*.html                 — переписки «как в Telegram» (см. render.py);
    переписки/*.txt             — те же переписки простым текстом;
    raw/archive.*.jsonl         — исходные записи (для программ);
    media/                      — вложения.

zip отправляется владельцу, после чего локальные копии удаляются.
messages.db (детекция удалений) не трогается.

Медиа выгружается порциями не больше лимита: один zip не превышает
EXPORT_LIMIT_MB, поэтому для сборки нужно мало свободного места, а
накопившийся «хвост» уходит несколькими файлами подряд.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import threading
import zipfile
from datetime import datetime

from render import Clock, render_export

log = logging.getLogger("tg-skaner")

# Запас под лимит Telegram на размер одного файла (~2 ГБ).
MAX_LIMIT_MB = 1900


def make_clock(tz_name: str | None) -> Clock:
    """Часовой пояс для времени в переписках (TIMEZONE), иначе — время сервера."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return Clock(ZoneInfo(tz_name), tz_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("TIMEZONE=%s не распознан (%s) — использую время сервера.", tz_name, exc)
    local = datetime.now().astimezone().tzname() or "?"
    return Clock(None, f"время сервера, {local}")


class Archiver:
    def __init__(
        self, export_dir: str, media_dir: str, limit_mb: int, tz_name: str | None = None
    ) -> None:
        self.export_dir = export_dir
        self.clock = make_clock(tz_name)
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

    def free_bytes(self) -> int:
        """Свободное место на диске, где лежит архив."""
        try:
            return shutil.disk_usage(self.export_dir).free
        except OSError:
            return 0

    # ─────────────────────────── экспорт ───────────────────────────────────
    def _list_media(self) -> list[str]:
        """Файлы медиа, от старых к новым (чтобы уходили в порядке появления)."""
        if not os.path.isdir(self.media_dir):
            return []
        items = []
        for n in os.listdir(self.media_dir):
            fp = os.path.join(self.media_dir, n)
            try:
                if os.path.isfile(fp):
                    items.append((os.path.getmtime(fp), fp))
            except OSError:
                pass
        return [fp for _, fp in sorted(items)]

    def _pick_media(self, budget: int) -> list[str]:
        """Берёт самые старые медиа, пока суммарно влезают в budget байт.
        Хотя бы один файл берём всегда — иначе крупный файл застрял бы навсегда."""
        picked: list[str] = []
        total = 0
        for m in self._list_media():
            try:
                size = os.path.getsize(m)
            except OSError:
                continue
            if picked and total + size > budget:
                break
            picked.append(m)
            total += size
        return picked

    def drop_oldest_media(self, need_bytes: int) -> tuple[int, int]:
        """Аварийно удаляет самые старые медиа, чтобы освободить need_bytes.
        Используется, только когда диск почти заполнен и иначе бот встанет.
        Возвращает (сколько файлов удалено, сколько байт освобождено)."""
        count = freed = 0
        for m in self._list_media():
            if freed >= need_bytes:
                break
            try:
                size = os.path.getsize(m)
                os.remove(m)
                count += 1
                freed += size
            except OSError:
                pass
        return count, freed

    def build_zip(self, media_budget: int | None = None):
        """Готовит zip к отправке (блокирующая операция — вызывать в to_thread).

        media_budget — сколько байт медиа положить в этот zip (по умолчанию —
        лимит экспорта). Остальное медиа уйдёт следующими файлами.

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
        if media_budget is None:
            media_budget = self.limit_bytes
        media_files = self._pick_media(media_budget)
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
            media_names = {os.path.basename(m) for m in media_files}
            stats = render_export(part_files, render_dir, media_names, self.clock)
            with zipfile.ZipFile(
                tmp_path, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
            ) as zf:
                for root, _dirs, files in os.walk(render_dir):
                    for name in sorted(files):
                        fp = os.path.join(root, name)
                        zf.write(fp, arcname=os.path.relpath(fp, render_dir))
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
