"""SQLite 持久化层。

只用 stdlib sqlite3：从 bot 的 asyncio 上下文里调用时用 run_in_executor 包装；
sync.py 用同步 API 直接调用。每张表只保留必须字段，避免随业务变化频繁迁移。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urls_seen (
    url        TEXT PRIMARY KEY,
    chat_id    TEXT,
    seen_at    REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'sent'
);
CREATE INDEX IF NOT EXISTS idx_urls_seen_at ON urls_seen(seen_at);

CREATE TABLE IF NOT EXISTS tweets (
    tweet_id      TEXT PRIMARY KEY,
    author        TEXT,
    tweet_text    TEXT,
    url           TEXT,
    files         TEXT,
    downloaded_at REAL,
    sent_at       REAL,
    sent_to       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author);
CREATE INDEX IF NOT EXISTS idx_tweets_downloaded ON tweets(downloaded_at);

CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
    tweet_id UNINDEXED,
    author,
    tweet_text,
    content=''
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    handle          TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    added_at        REAL NOT NULL,
    last_synced_at  REAL,
    UNIQUE(handle, chat_id)
);

CREATE TABLE IF NOT EXISTS failed_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL,
    chat_id         TEXT,
    dest_chat_id    TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 1,
    last_attempt_at REAL NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failed_created ON failed_queue(created_at);

CREATE TABLE IF NOT EXISTS media_hashes (
    md5            TEXT PRIMARY KEY,
    first_seen_at  REAL NOT NULL,
    tweet_id       TEXT,
    file_name      TEXT
);

CREATE TABLE IF NOT EXISTS bot_stats (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    """线程安全的轻量封装。所有方法都用同一把锁 + check_same_thread=False。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- URL 去重 ----------

    def url_seen(self, url: str, ttl_seconds: float) -> bool:
        """若 url 在 TTL 内已记录返回 True，否则记录并返回 False。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT seen_at FROM urls_seen WHERE url = ?", (url,)
            ).fetchone()
            if row and now - row["seen_at"] < ttl_seconds:
                return True
            self._conn.execute(
                "INSERT INTO urls_seen(url, seen_at, status) VALUES(?, ?, 'pending') "
                "ON CONFLICT(url) DO UPDATE SET seen_at=excluded.seen_at, status='pending'",
                (url, now),
            )
            self._conn.commit()
            return False

    def url_mark(self, url: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE urls_seen SET status=?, seen_at=? WHERE url=?",
                (status, time.time(), url),
            )
            self._conn.commit()

    def url_forget(self, url: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM urls_seen WHERE url=?", (url,))
            self._conn.commit()

    def url_prune(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        with self._lock:
            cur = self._conn.execute("DELETE FROM urls_seen WHERE seen_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ---------- tweets / search ----------

    def tweet_upsert(
        self,
        tweet_id: str,
        *,
        author: str = "",
        tweet_text: str = "",
        url: str = "",
        files: Iterable[str] = (),
        sent_to: str | None = None,
    ) -> None:
        now = time.time()
        files_json = json.dumps(list(files), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """INSERT INTO tweets(tweet_id, author, tweet_text, url, files, downloaded_at, sent_at, sent_to)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tweet_id) DO UPDATE SET
                     author=COALESCE(NULLIF(excluded.author, ''), tweets.author),
                     tweet_text=COALESCE(NULLIF(excluded.tweet_text, ''), tweets.tweet_text),
                     url=COALESCE(NULLIF(excluded.url, ''), tweets.url),
                     files=excluded.files,
                     sent_at=COALESCE(excluded.sent_at, tweets.sent_at),
                     sent_to=COALESCE(excluded.sent_to, tweets.sent_to)
                """,
                (tweet_id, author, tweet_text, url, files_json,
                 now, now if sent_to else None, sent_to),
            )
            self._conn.execute("DELETE FROM tweets_fts WHERE tweet_id = ?", (tweet_id,))
            if tweet_text or author:
                self._conn.execute(
                    "INSERT INTO tweets_fts(tweet_id, author, tweet_text) VALUES(?, ?, ?)",
                    (tweet_id, author, tweet_text),
                )
            self._conn.commit()

    def tweet_search(self, query: str, limit: int = 10) -> list[sqlite3.Row]:
        q = query.strip()
        if not q:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT t.tweet_id, t.author, t.tweet_text, t.url, t.downloaded_at
                       FROM tweets_fts f JOIN tweets t ON f.tweet_id = t.tweet_id
                       WHERE tweets_fts MATCH ?
                       ORDER BY t.downloaded_at DESC LIMIT ?""",
                    (q, limit),
                ).fetchall()
                return rows
            except sqlite3.OperationalError:
                like = f"%{q}%"
                return self._conn.execute(
                    """SELECT tweet_id, author, tweet_text, url, downloaded_at
                       FROM tweets
                       WHERE tweet_text LIKE ? OR author LIKE ?
                       ORDER BY downloaded_at DESC LIMIT ?""",
                    (like, like, limit),
                ).fetchall()

    def tweet_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

    # ---------- subscriptions ----------

    def sub_add(self, handle: str, chat_id: str) -> bool:
        handle = handle.lstrip("@").lower()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO subscriptions(handle, chat_id, added_at) VALUES(?, ?, ?)",
                    (handle, chat_id, time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def sub_remove(self, handle: str, chat_id: str) -> bool:
        handle = handle.lstrip("@").lower()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM subscriptions WHERE handle=? AND chat_id=?",
                (handle, chat_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def sub_list(self, chat_id: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if chat_id:
                return self._conn.execute(
                    "SELECT * FROM subscriptions WHERE chat_id=? ORDER BY handle", (chat_id,)
                ).fetchall()
            return self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY handle"
            ).fetchall()

    def sub_mark_synced(self, sub_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE subscriptions SET last_synced_at=? WHERE id=?",
                (time.time(), sub_id),
            )
            self._conn.commit()

    # ---------- failed queue ----------

    def failed_add(self, url: str, chat_id: str | None, dest: str | None, error: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO failed_queue(url, chat_id, dest_chat_id, error, attempts,
                                            last_attempt_at, created_at)
                   VALUES(?, ?, ?, ?, 1, ?, ?)""",
                (url, chat_id, dest, error[:500], now, now),
            )
            self._conn.commit()

    def failed_list(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM failed_queue ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def failed_pop_all(self) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM failed_queue ORDER BY created_at"
            ).fetchall()
            self._conn.execute("DELETE FROM failed_queue")
            self._conn.commit()
            return rows

    def failed_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM failed_queue").fetchone()[0]

    # ---------- media hash dedup ----------

    def media_hash_seen(self, md5: str, tweet_id: str = "", file_name: str = "") -> bool:
        """True 表示之前已经见过这个 hash。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM media_hashes WHERE md5=?", (md5,)
            ).fetchone()
            if row:
                return True
            self._conn.execute(
                "INSERT INTO media_hashes(md5, first_seen_at, tweet_id, file_name) VALUES(?, ?, ?, ?)",
                (md5, time.time(), tweet_id, file_name),
            )
            self._conn.commit()
            return False

    def media_hash_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM media_hashes").fetchone()[0]

    # ---------- stats kv ----------

    def stat_inc(self, key: str, delta: int = 1) -> None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM bot_stats WHERE key=?", (key,)).fetchone()
            cur = int(row["value"]) if row else 0
            self._conn.execute(
                "INSERT INTO bot_stats(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(cur + delta)),
            )
            self._conn.commit()

    def stat_get(self, key: str, default: str = "0") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM bot_stats WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def stat_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bot_stats(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()


_singleton: DB | None = None
_singleton_lock = threading.Lock()


def get(path: Path) -> DB:
    """进程内单例。"""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = DB(path)
        return _singleton
