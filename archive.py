"""Единый архив всех копий сообщений + автоэкспорт по лимиту размера.

Каждое проходящее сообщение дописывается строкой JSON в export/archive.jsonl.
Когда суммарный объём (archive.jsonl + *.part + папка media/) достигает лимита,
всё упаковывается в zip, отправляется владельцу через бота, а локальные копии
удаляются — чтобы освободить диск. messages.db (детекция удалений) не трогается.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
import zipfile
from datetime import datetime

log = logging.getLogger("tg-skaner")

# Запас под лимит Telegram на размер одного файла (~2 ГБ).
MAX_LIMIT_MB = 1900


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

    def build_zip(self):
        """Готовит zip к отправке (блокирующая операция — вызывать в to_thread).

        Возвращает (zip_path, written_parts, written_media) либо None, если
        экспортировать нечего. Пишем во временный файл и переименовываем —
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
        written_parts: list[str] = []
        written_media: list[str] = []
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in part_files:
                    try:
                        zf.write(p, arcname=os.path.basename(p))
                        written_parts.append(p)
                    except OSError:
                        pass
                for m in media_files:
                    try:
                        # медиа уже сжато — не тратим CPU на DEFLATE.
                        zf.write(
                            m,
                            arcname=os.path.join("media", os.path.basename(m)),
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
        return zip_path, written_parts, written_media

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
