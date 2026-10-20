"""Единый архив всех копий сообщений + автоэкспорт по лимиту размера.

Каждое проходящее сообщение дописывается строкой JSON в export/archive.jsonl.
Когда суммарный объём (archive.jsonl + папка media/) достигает лимита, всё
упаковывается в zip, отправляется владельцу через бота, а локальные копии
удаляются — чтобы освободить диск. messages.db (детекция удалений) не трогается.
"""
from __future__ import annotations

import glob
import json
import os
import zipfile
from datetime import datetime


class Archiver:
    def __init__(self, export_dir: str, media_dir: str, limit_mb: int) -> None:
        self.export_dir = export_dir
        self.media_dir = media_dir
        self.limit_bytes = max(1, limit_mb) * 1024 * 1024
        self.archive_path = os.path.join(export_dir, "archive.jsonl")
        os.makedirs(export_dir, exist_ok=True)

    # ─────────────────────────── запись ────────────────────────────────────
    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
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

    def total_bytes(self) -> int:
        size = self._dir_size(self.media_dir)
        try:
            size += os.path.getsize(self.archive_path)
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

        Текущий archive.jsonl переименовывается в .part, чтобы новые записи шли
        в свежий файл и не попали под удаление. Возвращает
        (zip_path, part_files, media_files) или None, если экспортировать нечего.
        """
        if os.path.exists(self.archive_path) and os.path.getsize(self.archive_path) > 0:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            os.replace(
                self.archive_path,
                os.path.join(self.export_dir, f"archive.{stamp}.part"),
            )

        part_files = sorted(glob.glob(os.path.join(self.export_dir, "archive.*.part")))
        media_files = self._list_media()
        if not part_files and not media_files:
            return None

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        zip_path = os.path.join(self.export_dir, f"tg-skaner-export-{ts}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in part_files:
                zf.write(p, arcname=os.path.basename(p))
            for m in media_files:
                zf.write(m, arcname=os.path.join("media", os.path.basename(m)))
        return zip_path, part_files, media_files

    def cleanup_after_send(
        self, part_files: list[str], media_files: list[str], zip_path: str
    ) -> None:
        """Удаляет локальные копии ПОСЛЕ успешной отправки (освобождает диск).

        Удаляются только те файлы, что попали в zip; записи/медиа, добавленные
        во время экспорта, остаются.
        """
        for p in [*part_files, *media_files, zip_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def discard_zip(self, zip_path: str) -> None:
        """Удаляет только zip (при неудачной отправке данные .part/media остаются)."""
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
