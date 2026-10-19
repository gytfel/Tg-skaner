"""Локальный кэш сообщений на SQLite.

Telegram при удалении присылает только id сообщений (без текста), поэтому мы
заранее сохраняем каждое проходящее через аккаунт сообщение. Когда приходит
событие удаления — достаём оригинал из этого кэша.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER,
    message_id      INTEGER,
    is_channel      INTEGER DEFAULT 0,
    sender_id       INTEGER,
    sender_name     TEXT,
    sender_username TEXT,
    chat_title      TEXT,
    text            TEXT,
    media_path      TEXT,
    outgoing        INTEGER DEFAULT 0,
    date            INTEGER,
    cached_at       INTEGER,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_chat_msg ON messages(chat_id, message_id);
CREATE INDEX IF NOT EXISTS idx_msg      ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_cached   ON messages(cached_at);
"""


class MessageStore:
    def __init__(self, path: str = "messages.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def add(self, **fields: Any) -> None:
        fields.setdefault("cached_at", int(time.time()))
        cols = ", ".join(fields)
        placeholders = ", ".join(["?"] * len(fields))
        self.conn.execute(
            f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({placeholders})",
            tuple(fields.values()),
        )
        self.conn.commit()

    def get_by_chat(self, chat_id: int, message_id: int) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        return cur.fetchone()

    def get_private(self, message_id: int) -> Optional[sqlite3.Row]:
        """Для личных чатов/базовых групп событие удаления не содержит чат —
        ищем по id среди не-канальных сообщений (id там уникален в рамках аккаунта)."""
        cur = self.conn.execute(
            "SELECT * FROM messages WHERE message_id = ? AND is_channel = 0 "
            "ORDER BY id DESC LIMIT 1",
            (message_id,),
        )
        return cur.fetchone()

    def update_text(self, chat_id: int, message_id: int, text: str) -> None:
        self.conn.execute(
            "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
            (text, chat_id, message_id),
        )
        self.conn.commit()

    def update_media(self, chat_id: int, message_id: int, path: str) -> None:
        """Дописывает путь к медиа после того, как оно скачалось (см. bot.py)."""
        self.conn.execute(
            "UPDATE messages SET media_path = ? WHERE chat_id = ? AND message_id = ?",
            (path, chat_id, message_id),
        )
        self.conn.commit()

    def cleanup(self, older_than_ts: int) -> list[str]:
        """Удаляет старые записи. Возвращает пути к медиа, которые пора стереть с диска."""
        cur = self.conn.execute(
            "SELECT media_path FROM messages "
            "WHERE cached_at < ? AND media_path IS NOT NULL",
            (older_than_ts,),
        )
        paths = [row["media_path"] for row in cur.fetchall() if row["media_path"]]
        self.conn.execute("DELETE FROM messages WHERE cached_at < ?", (older_than_ts,))
        self.conn.commit()
        return paths

    def close(self) -> None:
        self.conn.close()
