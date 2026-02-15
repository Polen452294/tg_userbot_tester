from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CacheEntry:
    value: str
    created_at: int


class SqliteTTLCache:
    """
    Простая TTL cache на SQLite.
    - Ключ: str
    - Значение: str (например, уже "безопасная выжимка")
    - created_at: unix seconds
    """

    def __init__(self, db_path: str, ttl_seconds: int):
        self.db_path = db_path
        self.ttl_seconds = int(ttl_seconds)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    async def get(self, key: str) -> Optional[CacheEntry]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, key)

    def _get_sync(self, key: str) -> Optional[CacheEntry]:
        cur = self._conn.execute("SELECT v, created_at FROM cache WHERE k = ?", (key,))
        row = cur.fetchone()
        if not row:
            return None
        v, created_at = row
        if self.ttl_seconds > 0 and (self._now() - int(created_at)) > self.ttl_seconds:
            # expire
            self._conn.execute("DELETE FROM cache WHERE k = ?", (key,))
            self._conn.commit()
            return None
        return CacheEntry(value=str(v), created_at=int(created_at))

    async def set(self, key: str, value: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._set_sync, key, value)

    def _set_sync(self, key: str, value: str) -> None:
        now = self._now()
        self._conn.execute(
            "INSERT INTO cache(k, v, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, created_at=excluded.created_at",
            (key, value, now),
        )
        self._conn.commit()

    async def purge_expired(self) -> int:
        """
        Удаляет протухшие записи, возвращает сколько удалено.
        """
        if self.ttl_seconds <= 0:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._purge_sync)

    def _purge_sync(self) -> int:
        cutoff = self._now() - self.ttl_seconds
        cur = self._conn.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount or 0

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)