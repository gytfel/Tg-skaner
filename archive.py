"""Единый архив всех копий сообщений + автоэкспорт по лимиту размера.

Каждое проходящее сообщение, а также события «изменено» и «удалено»
дописываются строкой JSON в export/archive.jsonl. Когда суммарный объём
(archive.jsonl + *.part + папка media/) достигает лимита, готовится экспорт:

    perepiska_<дата>.html            — все переписки одной страницей; бот шлёт
                                       её отдельным файлом, она открывается
                                       прямо в Telegram (фото встроены);
    perepiska_<дата>_vlozheniya.zip  — только если есть вложения:
        perepiska_<дата>.html        — та же страница, видео/голосовые играют;
        media/                       — вложения;
        raw/archive.*.jsonl          — исходные записи (для программ).

Файлы отправляются владельцу, после чего локальные копии удаляются.
messages.db (детекция удалений) не трогается.

Медиа выгружается порциями (до 1,5 × лимита на zip), поэтому для сборки нужно
мало свободного места, а накопившийся «хвост» уходит несколькими файлами.
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
        # Порция медиа в одном zip: с запасом над порогом, чтобы в обычном
        # режиме (порог только что превышен) всё уходило одним файлом и фото не
        # отрывались от своих сообщений. Делится только большой накопленный объём.
        self.chunk_bytes = min(self.limit_bytes * 3 // 2, MAX_LIMIT_MB * 1024 * 1024)
        self.archive_path = os.path.join(export_dir, "archive.jsonl")
        # threading.Lock: append() идёт из event-loop, build_export() — из рабочего
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

    def build_export(self, media_budget: int | None = None) -> dict | None:
        """Готовит экспорт к отправке (блокирующая операция — вызывать в to_thread).

        media_budget — сколько байт медиа взять в этот раз (по умолчанию —
        порция chunk_bytes). Остальное медиа уйдёт следующими экспортами.

        Возвращает словарь {html, zip, name, parts, media, stats} либо None, если
        экспортировать нечего. html/zip — пути к файлам для отправки (или None).
        Всё пишется во временные файлы и переименовывается — при ошибке
        (ENOSPC и т.п.) не остаётся битых файлов. Удаляются потом только те
        записи и медиа, что реально попали в экспорт.
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
            media_budget = self.chunk_bytes
        media_files = self._pick_media(media_budget)
        if not part_files and not media_files:
            return None

        name = "perepiska_" + datetime.now(self.clock.tz).strftime("%Y%m%d_%H%M%S")
        html_path = os.path.join(self.export_dir, f"{name}.html")
        zip_path = os.path.join(self.export_dir, f"{name}_vlozheniya.zip")
        work_dir = os.path.join(self.export_dir, f".render-{name}")
        page_in_zip = os.path.join(work_dir, f"{name}.html")
        media_paths = {os.path.basename(m): m for m in media_files}
        written_media: list[str] = []
        tmp_files = [html_path + ".tmp", zip_path + ".tmp"]
        try:
            os.makedirs(work_dir, exist_ok=True)
            stats = render_export(
                part_files,
                work_dir,
                self.clock,
                media_paths,
                phone_path=tmp_files[0] if part_files else None,
                archive_path=page_in_zip if (part_files and media_files) else None,
            )
            has_page = os.path.exists(tmp_files[0])
            if media_files:
                with zipfile.ZipFile(
                    tmp_files[1], "w", zipfile.ZIP_DEFLATED, strict_timestamps=False
                ) as zf:
                    if os.path.exists(page_in_zip):
                        zf.write(page_in_zip, arcname=f"{name}.html")
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
                            pass  # файл исчез во время упаковки — пропускаем
                    for p in part_files:
                        try:
                            raw = os.path.basename(p)[: -len(".part")] + ".jsonl"
                            zf.write(p, arcname=f"raw/{raw}")
                        except OSError:
                            pass
                os.replace(tmp_files[1], zip_path)
            if has_page:
                os.replace(tmp_files[0], html_path)
        except BaseException:
            for t in tmp_files:
                try:
                    if os.path.exists(t):
                        os.remove(t)
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        stats["media"] = len(written_media)
        return {
            "name": name,
            # html/zip — что ещё нужно отправить (бот обнуляет после отправки),
            # html_file/zip_file — что удалить с диска после доставки.
            "html": html_path if has_page else None,
            "zip": zip_path if media_files else None,
            "html_file": html_path if has_page else None,
            "zip_file": zip_path if media_files else None,
            # записи целиком вошли в страницу — после отправки их можно удалить
            "parts": list(part_files),
            "media": written_media,
            "stats": stats,
        }

    def cleanup_after_send(self, export: dict) -> None:
        """Удаляет локальные копии ПОСЛЕ успешной отправки (освобождает диск).

        Удаляются только записи и медиа, реально попавшие в экспорт; то, что
        добавилось во время отправки, остаётся до следующего раза.
        """
        files = [*export["parts"], *export["media"], export["html_file"], export["zip_file"]]
        for p in files:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    def clean_stale(self) -> None:
        """Убирает хвосты прошлого (упавшего) запуска: неотправленные/недособранные
        zip и временные папки. Данные не теряются — они остались в *.part и
        media/ и попадут в следующий экспорт."""
        for pattern in (
            "perepiska_*.html", "perepiska_*.zip", "perepiska_*.tmp",
            "tg-skaner-export-*.zip", "tg-skaner-export-*.zip.tmp",
        ):
            for p in glob.glob(os.path.join(self.export_dir, pattern)):
                try:
                    os.remove(p)
                except OSError:
                    pass
        for d in glob.glob(os.path.join(self.export_dir, ".render-*")):
            shutil.rmtree(d, ignore_errors=True)
