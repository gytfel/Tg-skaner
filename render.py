"""Оформление экспорта: читаемые переписки в HTML и TXT + оглавление.

Структура внутри zip:

    index.html                      — оглавление: все чаты, тип, id, счётчики;
    чаты/<Тип> - <Название> (<id>).html   — переписка «как в Telegram»
                                      (фото, видео и голосовые прямо в ленте);
    переписки/<Тип> - <Название> (<id>).txt — то же простым текстом;
    media/                          — вложения;
    raw/                            — исходные записи JSON (для программ).

Запись архива (строка JSON) бывает трёх видов: msg (сообщение), edit
(правка: было/стало) и del (удаление: оригинал из кэша). Чтение потоковое:
сначала собираем сведения о чатах (маленькие), потом пишем файлы.
"""
from __future__ import annotations

import html
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from urllib.parse import quote

# Сколько файлов держать открытыми одновременно при записи.
_MAX_OPEN = 64
# Сколько участников перечислять в шапке чата.
_MAX_PEOPLE = 300
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

CHAT_TYPES = {
    "private": ("👤", "Личный чат"),
    "bot": ("🤖", "Бот"),
    "saved": ("⭐", "Избранное"),
    "group": ("👥", "Группа"),
    "supergroup": ("👥", "Супергруппа"),
    "channel": ("📢", "Канал"),
}
_MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_VID = {".mp4", ".webm", ".mov", ".m4v"}
_AUD = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac"}


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


def _safe_name(name: str) -> str:
    return _BAD_CHARS.sub("_", name).strip(" .")[:60] or "chat"


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

    def time(self, ts) -> str:
        d = self.dt(ts)
        return d.strftime("%H:%M:%S") if d else "?"

    def full(self, ts) -> str:
        d = self.dt(ts)
        return d.strftime("%d.%m.%Y %H:%M:%S") if d else "?"

    def day_key(self, ts) -> str:
        d = self.dt(ts)
        return d.strftime("%Y-%m-%d") if d else "?"

    def day(self, ts) -> str:
        d = self.dt(ts)
        if not d:
            return "Дата неизвестна"
        return f"{d.day} {_MONTHS[d.month - 1]} {d.year}, {_WEEKDAYS[d.weekday()]}"


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
    def base_name(self) -> str:
        return f"{self.type_label} - {_safe_name(self.title)} ({self.key})"

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


def collect_meta(part_files: list[str]) -> dict[str, ChatMeta]:
    metas: dict[str, ChatMeta] = {}
    for r in iter_records(part_files):
        key = str(r.get("chat_id"))
        metas.setdefault(key, ChatMeta(key)).update(r)
    return metas


def _person_line(sid: str, p: dict) -> str:
    name = "Вы" if p["out"] else (p["name"] or "Без имени")
    if p["out"] and p["name"]:
        name += f" ({p['name']})"
    parts = []
    if p["username"]:
        parts.append(f"@{p['username']}")
    parts.append(f"id {sid}")
    if p["phone"]:
        parts.append(f"тел. +{str(p['phone']).lstrip('+')}")
    return f"{name} — " + " · ".join(parts)


def _sorted_people(meta: ChatMeta) -> list[tuple[str, dict]]:
    # Сначала собеседники (по активности), «Вы» — в конце.
    return sorted(meta.people.items(), key=lambda kv: (kv[1]["out"], -kv[1]["n"]))


def _sender_label(r: dict) -> str:
    if r.get("outgoing"):
        return "Вы"
    name = r.get("sender") or "Без имени"
    extra = []
    if r.get("username"):
        extra.append(f"@{r['username']}")
    if r.get("sender_id") is not None:
        extra.append(f"id {r['sender_id']}")
    return f"{name} ({' · '.join(extra)})" if extra else name


# ─────────────────────────────── TXT ────────────────────────────────────────

def _indent(text: str, n: int = 4) -> str:
    pad = " " * n
    return pad + text.replace("\n", "\n" + pad)


def txt_header(meta: ChatMeta, clock: Clock) -> str:
    line = "═" * 60
    rows = [
        f"{meta.icon} {meta.type_label}: {meta.title}",
        f"ID чата: {meta.key}" + (f"   Username: @{meta.username}" if meta.username else ""),
        f"Период: {clock.full(meta.first)} — {clock.full(meta.last)} ({clock.label})",
        f"В этом архиве: сообщений {meta.counts['msg']} · удалено {meta.counts['del']} · "
        f"изменено {meta.counts['edit']}",
        "Участники:",
    ]
    people = _sorted_people(meta)
    rows += [f"  • {_person_line(sid, p)}" for sid, p in people[:_MAX_PEOPLE]]
    if len(people) > _MAX_PEOPLE:
        rows.append(f"  … и ещё {len(people) - _MAX_PEOPLE}")
    return f"╔{line}\n" + "".join(f"║ {r}\n" for r in rows) + f"╚{line}\n"


def _txt_media(r: dict, media_names: set[str]) -> str | None:
    kind = r.get("media_kind") or "медиа"
    media = r.get("media")
    if not media:
        skipped = r.get("media_skipped")
        return f"📎 {kind} не сохранено: {skipped}" if skipped else None
    base = os.path.basename(media)
    name = f" «{r['file_name']}»" if r.get("file_name") else ""
    if base in media_names:
        return f"📎 {kind}{name}: media/{base}"
    return f"📎 {kind}{name}: media/{base} — в соседнем архиве"


def txt_record(r: dict, clock: Clock, media_names: set[str]) -> str:
    kind = r.get("type", "msg")
    num = f"#{r['msg_id']}" if r.get("msg_id") is not None else ""
    who = _sender_label(r)
    t = clock.time(r.get("ts"))
    if kind == "edit":
        return (
            f"[{t}] ✏️ ИЗМЕНЕНО {num} · {who}\n"
            f"    было:\n{_indent(r.get('old') or '(пусто)', 8)}\n"
            f"    стало:\n{_indent(r.get('new') or '(пусто)', 8)}\n"
        )
    lines = []
    if kind == "del":
        lines.append(
            f"[{t}] 🗑 УДАЛЕНО {num} · {who} · было отправлено {clock.full(r.get('orig_ts'))}"
        )
    else:
        lines.append(f"[{t}] {who} {num}".rstrip())
    extras = []
    if r.get("fwd"):
        extras.append(f"↪ переслано от: {r['fwd']}")
    if r.get("reply_to"):
        extras.append(f"↩ ответ на #{r['reply_to']}")
    if extras:
        lines.append("    " + " · ".join(extras))
    if r.get("text"):
        lines.append(_indent(r["text"]))
    media = _txt_media(r, media_names)
    if media:
        lines.append("    " + media)
    if len(lines) == 1 and kind != "del":
        lines.append("    (пусто)")
    return "\n".join(lines) + "\n"


# ─────────────────────────────── HTML ───────────────────────────────────────

_CSS = """
:root{--bg:#e6ebee;--panel:#fff;--in:#fff;--out:#e1f7d0;--text:#1c1c1e;--muted:#707b84;
--accent:#3390ec;--del:#e53935;--del-bg:#fdecea;--edit:#c77c00;--edit-bg:#fff6e0;--line:#d7dde2}
@media (prefers-color-scheme:dark){:root{--bg:#0e1621;--panel:#17212b;--in:#182533;--out:#2b5278;
--text:#e9edf1;--muted:#8a9aa9;--accent:#6ab3f3;--del:#ff6b66;--del-bg:#3a1f24;--edit:#f0b44c;
--edit-bg:#3a3020;--line:#24313d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
a{color:var(--accent)}
.wrap{max-width:820px;margin:0 auto;padding:0 12px 40px}
.top{position:sticky;top:0;z-index:2;background:var(--panel);border-bottom:1px solid var(--line)}
.top .wrap{display:flex;align-items:center;gap:12px;padding:10px 12px}
.back{text-decoration:none;font-size:14px;white-space:nowrap}
.ava{flex:none;width:44px;height:44px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:600;font-size:18px}
h1{margin:0;font-size:18px;line-height:1.2;word-break:break-word}
.sub{color:var(--muted);font-size:13px}
.info{background:var(--panel);border-radius:12px;margin:14px 0;padding:12px 16px;font-size:14px}
.info table{border-collapse:collapse}.info td{padding:2px 12px 2px 0;vertical-align:top}
.info td:first-child{color:var(--muted);white-space:nowrap}
.info details{margin-top:6px}.info summary{cursor:pointer;color:var(--accent)}
.info ul{margin:6px 0 0;padding-left:20px;max-height:320px;overflow:auto}
.day{text-align:center;margin:16px 0 8px}
.day span{background:rgba(0,0,0,.25);color:#fff;border-radius:12px;padding:3px 12px;font-size:13px}
.msg{max-width:85%;width:fit-content;margin:4px 0;padding:6px 10px 4px;border-radius:12px;background:var(--in);
box-shadow:0 1px 1px rgba(0,0,0,.08);word-wrap:break-word;overflow-wrap:anywhere}
.msg.out{margin-left:auto;background:var(--out)}
.who{font-weight:600;font-size:14px;color:var(--accent)}
.who small,.meta{font-weight:400;color:var(--muted);font-size:12px}
.text{white-space:pre-wrap}
.extra{font-size:13px;color:var(--muted);border-left:2px solid var(--accent);padding-left:6px;margin:2px 0 4px}
.time{text-align:right;color:var(--muted);font-size:12px;margin-top:2px}
.badge{display:inline-block;font-size:12px;font-weight:600;border-radius:6px;padding:1px 6px;margin-bottom:3px}
.msg.del{background:var(--del-bg);border:1px solid var(--del)}.msg.del .badge{color:#fff;background:var(--del)}
.msg.edit{background:var(--edit-bg);border:1px solid var(--edit)}.msg.edit .badge{color:#fff;background:var(--edit)}
.old{text-decoration:line-through;opacity:.75}
.lbl{font-size:12px;color:var(--muted);margin-top:4px}
.media img,.media video{display:block;max-width:100%;max-height:420px;border-radius:8px;margin:4px 0}
.media audio{width:100%;max-width:320px;margin:4px 0}
.note{font-size:13px;color:var(--muted);font-style:italic}
.file{display:inline-block;margin:4px 0;padding:6px 10px;border-radius:8px;background:rgba(127,127,127,.12);text-decoration:none}
.list{display:grid;gap:8px;margin:14px 0}
.item{display:flex;gap:12px;align-items:center;background:var(--panel);border-radius:12px;padding:10px 14px;text-decoration:none;color:inherit}
.item:hover{outline:2px solid var(--accent)}
.item .grow{flex:1;min-width:0}.item .t{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cnt{text-align:right;font-size:13px;color:var(--muted);white-space:nowrap}
.cnt b{color:var(--del)}.cnt i{color:var(--edit);font-style:normal}
.sum{background:var(--panel);border-radius:12px;padding:12px 16px;margin-top:14px;font-size:14px}
.txt{font-size:12px}
"""

_PALETTE = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _avatar(meta: ChatMeta) -> str:
    color = _PALETTE[zlib.crc32(meta.key.encode()) % len(_PALETTE)]
    letter = (meta.title.strip()[:1] or "?").upper()
    return f'<div class="ava" style="background:{color}">{_esc(letter)}</div>'


def _page_head(title: str) -> str:
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
    )


def html_header(meta: ChatMeta, clock: Clock) -> str:
    sub = [f"{meta.icon} {meta.type_label}", f"id {_esc(meta.key)}"]
    if meta.username:
        sub.append(f"@{_esc(meta.username)}")
    people = _sorted_people(meta)
    items = "".join(f"<li>{_esc(_person_line(sid, p))}</li>" for sid, p in people[:_MAX_PEOPLE])
    if len(people) > _MAX_PEOPLE:
        items += f"<li>… и ещё {len(people) - _MAX_PEOPLE}</li>"
    c = meta.counts
    rows = [
        ("Чат", _esc(meta.title)),
        ("Тип", f"{meta.icon} {meta.type_label}"),
        ("ID чата", f"<code>{_esc(meta.key)}</code>"),
    ]
    if meta.username:
        u = _esc(meta.username)
        rows.append(("Username", f'<a href="https://t.me/{u}">@{u}</a>'))
    rows += [
        ("Период", f"{clock.full(meta.first)} — {clock.full(meta.last)} ({_esc(clock.label)})"),
        ("В этом архиве", f"сообщений {c['msg']} · удалено {c['del']} · изменено {c['edit']}"),
    ]
    table = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    txt = quote(f"../переписки/{meta.base_name}.txt")
    return (
        _page_head(f"{meta.title} — Tg-skaner")
        + '<div class="top"><div class="wrap">'
        + '<a class="back" href="../index.html">← Все чаты</a>'
        + _avatar(meta)
        + f'<div><h1>{_esc(meta.title)}</h1><div class="sub">{" · ".join(sub)}</div></div>'
        + '</div></div><div class="wrap">'
        + f'<div class="info"><table>{table}</table>'
        + f"<details{' open' if len(people) <= 20 else ''}>"
        + f"<summary>Участники ({len(people)})</summary><ul>{items}</ul></details>"
        + f'<div class="txt"><a href="{txt}">Текстовая версия (.txt)</a></div></div>'
    )


HTML_FOOTER = "</div></body></html>\n"


def _html_media(r: dict, media_names: set[str]) -> str:
    kind = r.get("media_kind") or "медиа"
    media = r.get("media")
    if not media:
        skipped = r.get("media_skipped")
        if skipped:
            return f'<div class="note">📎 {_esc(kind)} не сохранено: {_esc(skipped)}</div>'
        return ""
    base = os.path.basename(media)
    label = r.get("file_name") or base
    if base not in media_names:
        return (
            f'<div class="note">📎 {_esc(kind)} «{_esc(label)}» — в соседнем архиве. '
            f'Распакуйте архивы в одну папку, и файл откроется по ссылке: '
            f'<a href="../media/{quote(base)}">{_esc(base)}</a></div>'
        )
    src = "../media/" + quote(base)
    ext = os.path.splitext(base)[1].lower()
    if ext in _IMG:
        el = f'<a href="{src}"><img src="{src}" loading="lazy" alt="{_esc(kind)}"></a>'
    elif ext in _VID:
        el = f'<video controls preload="metadata" src="{src}"></video>'
    elif ext in _AUD:
        el = f'<audio controls preload="none" src="{src}"></audio>'
    else:
        el = f'<a class="file" href="{src}">📄 {_esc(label)}</a>'
    return f'<div class="media">{el}</div>'


def _html_who(r: dict) -> str:
    if r.get("outgoing"):
        return '<div class="who">Вы</div>'
    extra = []
    if r.get("username"):
        extra.append(f"@{_esc(r['username'])}")
    if r.get("sender_id") is not None:
        extra.append(f"id {_esc(r['sender_id'])}")
    small = f" <small>{' · '.join(extra)}</small>" if extra else ""
    return f'<div class="who">{_esc(r.get("sender") or "Без имени")}{small}</div>'


def html_record(r: dict, clock: Clock, media_names: set[str]) -> str:
    kind = r.get("type", "msg")
    side = "out" if r.get("outgoing") else "in"
    mid = r.get("msg_id")
    num = f"#{_esc(mid)}" if mid is not None else ""
    time_ = f'<div class="time">{clock.time(r.get("ts"))} {num}</div>'
    who = _html_who(r)
    if kind == "edit":
        return (
            f'<div class="msg {side} edit">{who}<span class="badge">✏️ Изменено</span>'
            f'<div class="lbl">Было:</div><div class="text old">{_esc(r.get("old") or "(пусто)")}</div>'
            f'<div class="lbl">Стало:</div><div class="text">{_esc(r.get("new") or "(пусто)")}</div>'
            f"{time_}</div>\n"
        )
    extras = []
    if r.get("fwd"):
        extras.append(f"↪ Переслано от: {_esc(r['fwd'])}")
    if r.get("reply_to"):
        extras.append(f'↩ Ответ на <a href="#m{_esc(r["reply_to"])}">#{_esc(r["reply_to"])}</a>')
    extra = f'<div class="extra">{" · ".join(extras)}</div>' if extras else ""
    text = f'<div class="text">{_esc(r["text"])}</div>' if r.get("text") else ""
    media = _html_media(r, media_names)
    if kind == "del":
        badge = (
            f'<span class="badge">🗑 Удалено</span> '
            f'<span class="meta">было отправлено {clock.full(r.get("orig_ts"))}</span>'
        )
        body = text + media or '<div class="note">(без текста)</div>'
        return f'<div class="msg {side} del">{who}{badge}{extra}{body}{time_}</div>\n'
    body = text + media or '<div class="note">(пусто)</div>'
    anchor = f' id="m{_esc(mid)}"' if mid is not None else ""
    return f'<div class="msg {side}"{anchor}>{who}{extra}{body}{time_}</div>\n'


_EMPTY = (
    '<div class="sum">В этом архиве только вложения (папка media/) — они относятся '
    "к переписке из предыдущего архива.</div>"
)


def index_html(metas: dict[str, ChatMeta], clock: Clock, totals: dict) -> str:
    chats = sorted(metas.values(), key=lambda m: m.last or 0, reverse=True)
    firsts = [m.first for m in chats if m.first]
    lasts = [m.last for m in chats if m.last]
    period = f"{clock.full(min(firsts))} — {clock.full(max(lasts))}" if firsts else "—"
    items = []
    for m in chats:
        sub = [m.type_label, f"id {_esc(m.key)}"]
        if m.username:
            sub.append(f"@{_esc(m.username)}")
        c = m.counts
        cnt = f"{c['msg']} сообщ."
        if c["del"]:
            cnt += f"<br><b>🗑 {c['del']}</b>"
        if c["edit"]:
            cnt += f"<br><i>✏️ {c['edit']}</i>"
        href = quote(f"чаты/{m.base_name}.html")
        items.append(
            f'<a class="item" href="{href}">{_avatar(m)}<div class="grow">'
            f'<div class="t">{m.icon} {_esc(m.title)}</div>'
            f'<div class="sub">{" · ".join(sub)} · последнее: {clock.full(m.last)}</div></div>'
            f'<div class="cnt">{cnt}</div></a>'
        )
    return (
        _page_head("Tg-skaner — архив переписок")
        + '<div class="top"><div class="wrap"><div><h1>📦 Архив переписок Tg-skaner</h1>'
        + f'<div class="sub">Чатов: {len(chats)} · {period} ({_esc(clock.label)})</div>'
        + '</div></div></div><div class="wrap"><div class="sum">'
        + f"Сообщений: {totals.get('msg', 0)} · удалено: <b>{totals.get('del', 0)}</b> · "
        + f"изменено: {totals.get('edit', 0)} · медиа-файлов: {totals.get('media', 0)}<br>"
        + '<span class="sub">Удалённые сообщения отмечены красным, изменённые — жёлтым. '
        + "Текстовые версии — в папке «переписки».</span></div>"
        + f'<div class="list">{"".join(items) or _EMPTY}</div>'
        + HTML_FOOTER
    )


# ─────────────────────────────── сборка ─────────────────────────────────────

def render_export(
    part_files: list[str], out_dir: str, media_names: set[str], clock: Clock
) -> dict:
    """Пишет index.html, чаты/*.html и переписки/*.txt в out_dir.
    Возвращает счётчики записей {msg, edit, del}."""
    metas = collect_meta(part_files)
    html_dir = os.path.join(out_dir, "чаты")
    txt_dir = os.path.join(out_dir, "переписки")
    os.makedirs(html_dir, exist_ok=True)
    os.makedirs(txt_dir, exist_ok=True)

    stats = {"msg": 0, "edit": 0, "del": 0}
    handles: dict[str, tuple] = {}
    created: set[str] = set()
    last_day: dict[str, str] = {}

    def open_pair(meta: ChatMeta):
        if len(handles) >= _MAX_OPEN:
            for h, t in handles.values():
                h.close()
                t.close()
            handles.clear()
        hp = os.path.join(html_dir, meta.base_name + ".html")
        tp = os.path.join(txt_dir, meta.base_name + ".txt")
        h = open(hp, "a", encoding="utf-8")
        t = open(tp, "a", encoding="utf-8")
        if meta.key not in created:
            created.add(meta.key)
            h.write(html_header(meta, clock))
            t.write(txt_header(meta, clock) + "\n")
        handles[meta.key] = (h, t)
        return h, t

    try:
        for r in iter_records(part_files):
            meta = metas[str(r.get("chat_id"))]
            h, t = handles.get(meta.key) or open_pair(meta)
            day = clock.day_key(r.get("ts"))
            if last_day.get(meta.key) != day:
                last_day[meta.key] = day
                label = clock.day(r.get("ts"))
                h.write(f'<div class="day"><span>{_esc(label)}</span></div>\n')
                t.write(f"\n──────────── {label} ────────────\n\n")
            h.write(html_record(r, clock, media_names))
            t.write(txt_record(r, clock, media_names) + "\n")
            kind = r.get("type", "msg")
            if kind in stats:
                stats[kind] += 1
    finally:
        for h, t in handles.values():
            h.close()
            t.close()

    for meta in metas.values():
        with open(os.path.join(html_dir, meta.base_name + ".html"), "a", encoding="utf-8") as h:
            h.write(HTML_FOOTER)

    totals = {**stats, "media": len(media_names)}
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html(metas, clock, totals))
    return stats
