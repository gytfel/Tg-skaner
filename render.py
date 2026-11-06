"""Оформление экспорта: одна HTML-страница со всеми переписками.

Делаем два варианта одной и той же страницы:

* «на телефон» — самодостаточный файл perepiska_<дата>.html: бот присылает
  его отдельно, он открывается прямо в Telegram (без распаковки). Фото
  встроены в сам файл (в пределах лимита), про видео/голосовые/файлы — пометка,
  что они в архиве вложений;
* «в архив» — та же страница внутри zip с папкой media/: там фото, видео и
  голосовые показываются и проигрываются прямо в ленте.

Запись архива (строка JSON) бывает трёх видов: msg (сообщение), edit
(правка: было/стало) и del (удаление: оригинал из кэша). Чтение потоковое:
сначала собираем сведения о чатах (маленькие), затем пишем сообщения каждого
чата во временный фрагмент и в конце склеиваем страницу.
"""
from __future__ import annotations

import base64
import html
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from urllib.parse import quote

# Сколько файлов держать открытыми одновременно при записи.
_MAX_OPEN = 64
# Сколько участников перечислять у группы.
_MAX_PEOPLE = 300
# Фото, встроенные в файл «на телефон»: не больше стольких байт всего и на одно.
EMBED_TOTAL = 20 * 1024 * 1024
EMBED_ONE = 3 * 1024 * 1024
# Сколько сообщений помнить для цитаты в ответах и длина цитаты.
_MAX_SNIPPETS = 200_000
_SNIPPET_LEN = 90

CHAT_TYPES = {
    "private": ("👤", "Личный чат"),
    "bot": ("🤖", "Бот"),
    "saved": ("⭐", "Избранное"),
    "group": ("👥", "Группа"),
    "supergroup": ("👥", "Супергруппа"),
    "channel": ("📢", "Канал"),
}
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
_VID = {".mp4", ".webm", ".mov", ".m4v"}
_AUD = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac"}
_KIND_ICON = {
    "фото": "🖼", "видео": "🎬", "кружок": "🎬", "GIF": "🎞", "голосовое": "🎤",
    "аудио": "🎵", "стикер": "🧩", "файл": "📄", "контакт": "👤",
    "геопозиция": "📍", "опрос": "📊",
}


# ─────────────────────────── общие помощники ────────────────────────────────

def chat_type(r: dict) -> str:
    """Тип чата из записи; для старых записей без типа — по виду id."""
    t = r.get("chat_type")
    if t in CHAT_TYPES:
        return t
    try:
        cid = int(r.get("chat_id"))
    except (TypeError, ValueError):
        return "private"
    if cid > 0:
        return "private"
    return "supergroup" if str(cid).startswith("-100") else "group"


def iter_records(part_files: list[str]):
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
                if isinstance(r, dict):
                    yield r


class Clock:
    """Форматирование времени в выбранном часовом поясе."""

    def __init__(self, tz: tzinfo | None, tz_label: str) -> None:
        self.tz = tz
        self.label = tz_label

    def dt(self, ts) -> datetime | None:
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(int(ts), self.tz)
        except (OverflowError, OSError, TypeError, ValueError):
            return None

    def full(self, ts) -> str:
        d = self.dt(ts)
        return d.strftime("%d.%m.%Y %H:%M") if d else "?"

    def now(self) -> str:
        return datetime.now(self.tz).strftime("%d.%m.%Y %H:%M")


@dataclass
class ChatMeta:
    key: str
    title: str = "—"
    type: str = "private"
    username: str | None = None
    first: int | None = None
    last: int | None = None
    counts: dict = field(default_factory=lambda: {"msg": 0, "edit": 0, "del": 0})
    people: dict = field(default_factory=dict)  # sender_id -> сведения

    @property
    def icon(self) -> str:
        return CHAT_TYPES[self.type][0]

    @property
    def type_label(self) -> str:
        return CHAT_TYPES[self.type][1]

    @property
    def is_private(self) -> bool:
        return self.type in ("private", "bot", "saved")

    def update(self, r: dict) -> None:
        if r.get("chat"):
            self.title = r["chat"]
        if r.get("chat_type") in CHAT_TYPES or self.type == "private":
            self.type = chat_type(r)
        if r.get("chat_username"):
            self.username = r["chat_username"]
        ts = r.get("ts")
        if isinstance(ts, (int, float)):
            self.first = ts if self.first is None else min(self.first, ts)
            self.last = ts if self.last is None else max(self.last, ts)
        kind = r.get("type", "msg")
        if kind in self.counts:
            self.counts[kind] += 1
        sid = r.get("sender_id")
        if sid is not None:
            p = self.people.setdefault(
                str(sid), {"name": None, "username": None, "phone": None, "out": False, "n": 0}
            )
            p["name"] = r.get("sender") or p["name"]
            p["username"] = r.get("username") or p["username"]
            p["phone"] = r.get("phone") or p["phone"]
            p["out"] = p["out"] or bool(r.get("outgoing"))
            p["n"] += 1


def _snippet(r: dict) -> str:
    text = " ".join((r.get("text") or "").split())
    if not text:
        kind = r.get("media_kind")
        return f"[{kind}]" if kind else "сообщение"
    return text if len(text) <= _SNIPPET_LEN else text[: _SNIPPET_LEN - 1] + "…"


def collect(part_files: list[str]) -> tuple[dict[str, ChatMeta], dict]:
    """Первый проход: сведения о чатах и короткие цитаты для ответов."""
    metas: dict[str, ChatMeta] = {}
    snippets: dict[tuple, tuple[str, str]] = {}
    for r in iter_records(part_files):
        key = str(r.get("chat_id"))
        metas.setdefault(key, ChatMeta(key)).update(r)
        if r.get("type", "msg") == "msg" and len(snippets) < _MAX_SNIPPETS:
            who = "Вы" if r.get("outgoing") else (r.get("sender") or "?")
            snippets[(key, r.get("msg_id"))] = (who, _snippet(r))
    return metas, snippets


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _person(sid: str, p: dict) -> str:
    name = "Вы" if p["out"] else (p["name"] or "Без имени")
    if p["out"] and p["name"]:
        name += f" ({p['name']})"
    parts = [f"<b>{_esc(name)}</b>"]
    if p["username"]:
        parts.append(f"@{_esc(p['username'])}")
    parts.append(f"id {_esc(sid)}")
    if p["phone"]:
        parts.append(f"тел. +{_esc(str(p['phone']).lstrip('+'))}")
    return " · ".join(parts)


def _sorted_people(meta: ChatMeta) -> list[tuple[str, dict]]:
    # Сначала собеседники (по активности), «Вы» — в конце.
    return sorted(meta.people.items(), key=lambda kv: (kv[1]["out"], -kv[1]["n"]))


# ─────────────────────────────── страница ───────────────────────────────────

_CSS = """
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0f1720;color:#f2f5f8;font:17px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:22px 16px 48px}
a{color:#6cb6ff;text-decoration:none}
h1{font-size:30px;margin:4px 0 10px}
.gen{color:#8d9aa8;font-size:15px;margin-bottom:6px}
.legend{color:#8d9aa8;font-size:14px;margin-bottom:18px}
.toc{display:grid;gap:8px;margin:0 0 26px}
.toc a{display:flex;justify-content:space-between;gap:10px;background:#18222d;border-radius:14px;padding:12px 14px;color:#f2f5f8}
.toc .n{color:#8d9aa8;font-size:14px;white-space:nowrap}
.chat{margin-top:30px}
h2{font-size:24px;margin:0;padding-bottom:10px;border-bottom:1px solid #263443;color:#6cb6ff;word-break:break-word}
.info{color:#8d9aa8;font-size:14px;margin:8px 0 14px;line-height:1.6}
.info b{color:#c9d3dc;font-weight:600}
details summary{cursor:pointer;color:#6cb6ff}
details ul{margin:6px 0 0;padding-left:18px}
.m{background:#18222d;border-radius:14px;padding:12px 14px;margin:10px 0;overflow-wrap:anywhere}
.m.out{background:#1d3147}
.h{color:#8d9aa8;font-size:15px;margin-bottom:4px}
.t{white-space:pre-wrap}
.x{color:#8d9aa8;font-size:14px;border-left:3px solid #3f6f9c;padding-left:8px;margin:4px 0 6px}
.badge{display:inline-block;font-size:13px;font-weight:700;border-radius:7px;padding:2px 8px;margin:2px 0 6px}
.m.del{border:1px solid #e5534b;background:#2a1b20}.m.del .badge{background:#e5534b;color:#fff}
.m.edit{border:1px solid #d29922;background:#2a2518}.m.edit .badge{background:#d29922;color:#111}
.lbl{color:#8d9aa8;font-size:13px;margin-top:6px}
.old{text-decoration:line-through;opacity:.7}
.md img,.md video{display:block;max-width:100%;max-height:480px;border-radius:10px;margin:6px 0}
.md audio{width:100%;margin:6px 0}
.note{color:#8d9aa8;font-size:14px;font-style:italic;margin-top:4px}
.file{display:inline-block;background:#223041;border-radius:10px;padding:8px 12px;margin:6px 0}
"""


class _Media:
    """Как показать вложение: встроить (телефон) или сослаться на media/ (архив)."""

    def __init__(self, media_paths: dict[str, str], embed: bool) -> None:
        self.paths = media_paths
        self.embed = embed
        self.embedded = 0

    def render(self, r: dict) -> str:
        kind = r.get("media_kind") or "медиа"
        icon = _KIND_ICON.get(kind, "📎")
        media = r.get("media")
        if not media:
            skipped = r.get("media_skipped")
            if skipped:
                return f'<div class="note">{icon} {_esc(kind)} не сохранено: {_esc(skipped)}</div>'
            return ""
        base = os.path.basename(media)
        label = r.get("file_name") or base
        path = self.paths.get(base)
        if path is None:
            return f'<div class="note">{icon} {_esc(kind)} «{_esc(label)}» — в соседнем архиве вложений</div>'
        ext = os.path.splitext(base)[1].lower()
        if self.embed:
            return self._embedded(path, ext, icon, kind, label)
        src = "media/" + quote(base)
        if ext in _MIME:
            el = f'<a href="{src}"><img src="{src}" loading="lazy" alt="{_esc(kind)}"></a>'
        elif ext in _VID:
            el = f'<video controls preload="metadata" src="{src}"></video>'
        elif ext in _AUD:
            el = f'<audio controls preload="none" src="{src}"></audio>'
        else:
            el = f'<a class="file" href="{src}">{icon} {_esc(label)}</a>'
        return f'<div class="md">{el}</div>'

    def _embedded(self, path: str, ext: str, icon: str, kind: str, label: str) -> str:
        note = f'<div class="note">{icon} {_esc(kind)} «{_esc(label)}» — в архиве вложений</div>'
        if ext not in _MIME:
            return note
        try:
            size = os.path.getsize(path)
            if size > EMBED_ONE or self.embedded + size > EMBED_TOTAL:
                return note
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            return note
        self.embedded += size
        return f'<div class="md"><img src="data:{_MIME[ext]};base64,{data}" alt="{_esc(kind)}"></div>'


def _anchor(key: str, mid) -> str:
    return f"m{key.replace('-', '_')}_{mid}"


def _who(r: dict, meta: ChatMeta) -> str:
    if r.get("outgoing"):
        return "Вы"
    name = _esc(r.get("sender") or meta.title or "Без имени")
    if r.get("username"):
        name += f" (@{_esc(r['username'])})"
    # В группах отправители разные — показываем id каждого; в личке id в шапке.
    if not meta.is_private and r.get("sender_id") is not None:
        name += f" · id {_esc(r['sender_id'])}"
    return name


def card(r: dict, meta: ChatMeta, clock: Clock, media: _Media, snippets: dict) -> str:
    kind = r.get("type", "msg")
    side = " out" if r.get("outgoing") else ""
    who = _who(r, meta)
    if kind == "edit":
        return (
            f'<div class="m edit{side}"><div class="h">{who} · {clock.full(r.get("ts"))}</div>'
            '<span class="badge">✏️ Изменено</span>'
            f'<div class="lbl">Было:</div><div class="t old">{_esc(r.get("old") or "(пусто)")}</div>'
            f'<div class="lbl">Стало:</div><div class="t">{_esc(r.get("new") or "(пусто)")}</div></div>\n'
        )

    extras = []
    if r.get("fwd"):
        extras.append(f'<div class="x">↪ Переслано от: {_esc(r["fwd"])}</div>')
    if r.get("reply_to"):
        ref = snippets.get((meta.key, r["reply_to"]))
        target = f"#{_anchor(meta.key, r['reply_to'])}"
        if ref:
            extras.append(
                f'<div class="x"><a href="{target}">↩ {_esc(ref[0])}:</a> {_esc(ref[1])}</div>'
            )
        else:
            extras.append(f'<div class="x">↩ Ответ на сообщение #{_esc(r["reply_to"])}</div>')
    text = f'<div class="t">{_esc(r["text"])}</div>' if r.get("text") else ""
    body = "".join(extras) + text + media.render(r)

    if kind == "del":
        return (
            f'<div class="m del{side}"><div class="h">{who} · {clock.full(r.get("orig_ts"))}</div>'
            f'<span class="badge">🗑 Удалено {clock.full(r.get("ts"))}</span>'
            f'{body or "<div class=note>(без текста)</div>"}</div>\n'
        )
    mid = r.get("msg_id")
    anchor = f' id="{_anchor(meta.key, mid)}"' if mid is not None else ""
    return (
        f'<div class="m{side}"{anchor}><div class="h">{who} · {clock.full(r.get("ts"))}</div>'
        f'{body or "<div class=note>(пусто)</div>"}</div>\n'
    )


def chat_head(meta: ChatMeta, n: int, clock: Clock) -> str:
    sub = [meta.type_label, f"id {_esc(meta.key)}"]
    if meta.username:
        u = _esc(meta.username)
        sub.append(f'<a href="https://t.me/{u}">@{u}</a>')
    c = meta.counts
    lines = [
        " · ".join(sub),
        f"Сообщений: {c['msg']} · 🗑 удалено: {c['del']} · ✏️ изменено: {c['edit']}",
        f"Период: {clock.full(meta.first)} — {clock.full(meta.last)}",
    ]
    people = _sorted_people(meta)
    if meta.is_private:
        lines += [_person(sid, p) for sid, p in people]
        extra = ""
    else:
        items = "".join(f"<li>{_person(sid, p)}</li>" for sid, p in people[:_MAX_PEOPLE])
        if len(people) > _MAX_PEOPLE:
            items += f"<li>… и ещё {len(people) - _MAX_PEOPLE}</li>"
        extra = f"<details><summary>Участники ({len(people)})</summary><ul>{items}</ul></details>"
    return (
        f'<section class="chat" id="c{n}"><h2>{meta.icon} {_esc(meta.title)}</h2>'
        f'<div class="info">{"<br>".join(lines)}{extra}</div>\n'
    )


def page_head(chats: list[ChatMeta], stats: dict, clock: Clock) -> str:
    toc = ""
    if len(chats) > 1:
        rows = []
        for n, m in enumerate(chats, 1):
            c = m.counts
            cnt = f"{c['msg']}"
            if c["del"]:
                cnt += f" · 🗑 {c['del']}"
            if c["edit"]:
                cnt += f" · ✏️ {c['edit']}"
            rows.append(
                f'<a href="#c{n}"><span>{m.icon} {_esc(m.title)}</span><span class="n">{cnt}</span></a>'
            )
        toc = f'<div class="toc">{"".join(rows)}</div>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        f"<title>Экспорт переписки</title><style>{_CSS}</style></head><body><div class=\"wrap\">"
        "<h1>Экспорт переписки</h1>"
        f'<div class="gen">Сформировано: {clock.now()} ({_esc(clock.label)}). '
        f"Чатов: {len(chats)} · сообщений: {stats['msg']} · удалено: {stats['del']} · "
        f"изменено: {stats['edit']}.</div>"
        '<div class="legend">🗑 — удалённые сообщения (с текстом, который был), '
        "✏️ — изменённые (было / стало).</div>"
        f"{toc}"
    )


PAGE_FOOTER = "</div></body></html>\n"


# ─────────────────────────────── сборка ─────────────────────────────────────

def render_export(
    part_files: list[str],
    work_dir: str,
    clock: Clock,
    media_paths: dict[str, str],
    phone_path: str | None,
    archive_path: str | None,
) -> dict:
    """Пишет страницу «на телефон» (phone_path, фото встроены) и/или страницу
    «в архив» (archive_path, ссылки на media/). Возвращает счётчики:
    msg/edit/del, chats, first/last (время первой и последней записи)."""
    metas, snippets = collect(part_files)
    chats = sorted(metas.values(), key=lambda m: m.last or 0, reverse=True)
    stats = {"msg": 0, "edit": 0, "del": 0}
    for m in chats:
        for k in stats:
            stats[k] += m.counts[k]
    firsts = [m.first for m in chats if m.first]
    lasts = [m.last for m in chats if m.last]
    stats.update(chats=len(chats), first=min(firsts, default=None), last=max(lasts, default=None))

    variants = []  # (итоговый путь, префикс фрагментов, способ показа медиа)
    if phone_path:
        variants.append((phone_path, "p", _Media(media_paths, embed=True)))
    if archive_path:
        variants.append((archive_path, "a", _Media(media_paths, embed=False)))
    if not variants or not chats:
        return stats

    frag_dir = os.path.join(work_dir, "frag")
    os.makedirs(frag_dir, exist_ok=True)
    index = {m.key: n for n, m in enumerate(chats, 1)}
    handles: dict[tuple, object] = {}

    def frag(prefix: str, key: str):
        fh = handles.get((prefix, key))
        if fh is None:
            if len(handles) >= _MAX_OPEN:
                for h in handles.values():
                    h.close()
                handles.clear()
            path = os.path.join(frag_dir, f"{prefix}{index[key]}.html")
            fh = handles[(prefix, key)] = open(path, "a", encoding="utf-8")
        return fh

    try:
        for r in iter_records(part_files):
            meta = metas[str(r.get("chat_id"))]
            for _, prefix, media in variants:
                frag(prefix, meta.key).write(card(r, meta, clock, media, snippets))
    finally:
        for h in handles.values():
            h.close()

    head = page_head(chats, stats, clock)
    for out_path, prefix, _ in variants:
        with open(out_path, "w", encoding="utf-8") as out:
            out.write(head)
            for n, meta in enumerate(chats, 1):
                out.write(chat_head(meta, n, clock))
                fp = os.path.join(frag_dir, f"{prefix}{n}.html")
                if os.path.exists(fp):
                    with open(fp, encoding="utf-8") as f:
                        shutil.copyfileobj(f, out)
                out.write("</section>\n")
            out.write(PAGE_FOOTER)
    shutil.rmtree(frag_dir, ignore_errors=True)
    return stats
