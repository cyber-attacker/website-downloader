"""Persistent crawl state: the urls table IS the queue (and its history).

Schema (build guide §4):
    urls(id, url UNIQUE, status, depth, content_type, local_path,
         content_hash, etag, last_modified, retry_count, error,
         discovered_at, downloaded_at)
    project_meta(key, value)      -- one project's settings
    link_edges(from_id, to_id)   -- which page referenced which URL

Status lifecycle: pending -> downloading -> done | failed | skipped.
A crash can only ever leave rows in `pending`/`downloading`; on the next run
those are re-queued, `done` rows are left on disk alone, so a resumed crawl
never re-downloads finished work.

Every write is its own small transaction (the guide's crash-safety rule):
the more often we commit, the less a crash can ever lose.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'pending',
    depth         INTEGER NOT NULL DEFAULT 0,
    content_type  TEXT,
    local_path    TEXT,
    content_hash  TEXT,
    etag          TEXT,
    last_modified TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    discovered_at REAL NOT NULL,
    downloaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status);
CREATE INDEX IF NOT EXISTS idx_urls_depth  ON urls(depth);

CREATE TABLE IF NOT EXISTS project_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS link_edges (
    from_id INTEGER NOT NULL,
    to_id   INTEGER NOT NULL,
    PRIMARY KEY (from_id, to_id)
);
"""


def content_hash_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PendingURL:
    url: str
    depth: int
    referer: str | None = None


class StateDB:
    """Thin async wrapper: the only module allowed to speak SQL."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        if self._db is not None:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path, timeout=15.0)
        self._db.row_factory = aiosqlite.Row
        try:
            await self._db.execute("PRAGMA busy_timeout=10000")
            try:
                # crash-safe + concurrent reads; may transiently fail when a
                # concurrent connection holds the write lock (already-WAL dbs)
                await self._db.execute("PRAGMA journal_mode=WAL")
            except aiosqlite.OperationalError:
                pass
            await self._db.execute("PRAGMA synchronous=NORMAL")  # fast + still fsyncs on commit
            await self._db.executescript(SCHEMA)
            await self._db.commit()
        except Exception:
            # never leak the connection thread — it is non-daemon and would
            # keep the whole process alive after the caller gives up
            db, self._db = self._db, None
            await db.close()
            raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.commit()
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("StateDB.open() must be awaited before use")
        return self._db

    # ---- project_meta ------------------------------------------------------

    async def save_meta(self, meta: dict[str, Any]) -> None:
        await self.db.executemany(
            "INSERT INTO project_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()],
        )
        await self.db.commit()

    async def load_meta(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with self.db.execute("SELECT key, value FROM project_meta") as cur:
            async for row in cur:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    out[row["key"]] = row["value"]
        return out

    async def bump_last_run(self) -> None:
        await self.save_meta({"last_run_at": time.time()})

    # ---- urls --------------------------------------------------------------

    async def ensure_url(self, url: str, depth: int, referer: str | None = None) -> int:
        """Insert a discovered URL as `pending` unless a row already exists.
        Returns the row id."""
        assert url, "empty url"
        await self.db.execute(
            "INSERT INTO urls(url, depth, status, discovered_at) "
            "VALUES(?, ?, 'pending', ?) "
            "ON CONFLICT(url) DO NOTHING",
            (url, depth, time.time()),
        )
        await self.db.commit()
        row = await self._row_for(url)
        row_id = int(row["id"]) if row else -1
        if referer and row_id > 0:
            await self._add_edge(referer, row_id)
        return row_id

    async def _row_for(self, url: str) -> aiosqlite.Row | None:
        async with self.db.execute("SELECT * FROM urls WHERE url = ?", (url,)) as cur:
            return await cur.fetchone()

    async def _add_edge(self, from_url: str, to_id: int) -> None:
        row = await self._row_for(from_url)
        if row is None:
            return
        await self.db.execute(
            "INSERT INTO link_edges(from_id, to_id) VALUES(?, ?) "
            "ON CONFLICT DO NOTHING",
            (row["id"], to_id),
        )
        await self.db.commit()

    async def mark_status(self, url: str, status: str, **fields: Any) -> None:
        """Small one-row transaction: the crash-safety unit of the whole design."""
        if status == "done":
            fields.setdefault("downloaded_at", time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE urls SET status = ?{', ' + cols if cols else ''} WHERE url = ?"
        await self.db.execute(sql, (status, *fields.values(), url))
        await self.db.commit()

    async def record_failure(self, url: str, error: str, retry_count: int,
                             max_retries: int) -> str:
        """Returns the status the row ended in: 'pending' (will be retried) or
        'failed' (retry budget exhausted)."""
        status = "pending" if retry_count < max_retries else "failed"
        await self.mark_status(url, status, error=error, retry_count=retry_count)
        return status

    async def record_skip(self, url: str, depth: int, reason: str) -> None:
        await self.db.execute(
            "INSERT INTO urls(url, depth, status, error, discovered_at) "
            "VALUES(?, ?, 'skipped', ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET status='skipped', error=excluded.error "
            "WHERE urls.status != 'done'",
            (url, depth, reason, time.time()),
        )
        await self.db.commit()

    async def get_pending(self) -> list[PendingURL]:
        """Everything that must be (re-)queued on startup: never-finished work."""
        out: list[PendingURL] = []
        async with self.db.execute(
            "SELECT url, depth FROM urls WHERE status IN ('pending', 'downloading') "
            "ORDER BY depth, discovered_at"
        ) as cur:
            async for row in cur:
                out.append(PendingURL(row["url"], row["depth"]))
        return out

    async def skipped_or_failed_urls(self) -> set[str]:
        """URLs a previous run deliberately left out of the mirror — the resume
        run must treat them exactly like fresh out-of-scope discoveries."""
        out: set[str] = set()
        async with self.db.execute(
            "SELECT url FROM urls WHERE status IN ('skipped', 'failed')"
        ) as cur:
            async for row in cur:
                out.add(row["url"])
        return out

    async def all_known_urls(self) -> set[str]:
        out: set[str] = set()
        async with self.db.execute("SELECT url FROM urls") as cur:
            async for row in cur:
                out.add(row["url"])
        return out

    async def hydrate_records(self) -> list[dict[str, Any]]:
        """Rows saved as `done` in a previous run, as plain dicts — enough to
        rebuild the in-memory ResourceRecords the manifest/verify passes need."""
        out: list[dict[str, Any]] = []
        async with self.db.execute(
            "SELECT url, depth, content_type, local_path, content_hash, "
            "etag, last_modified, downloaded_at FROM urls WHERE status = 'done'"
        ) as cur:
            async for row in cur:
                out.append(dict(row))
        return out

    async def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        async with self.db.execute(
            "SELECT status, COUNT(*) AS n FROM urls GROUP BY status"
        ) as cur:
            async for row in cur:
                out[row["status"]] = row["n"]
        return out

    # ---- link graph (GUI phase 4) -------------------------------------------

    async def edges_for_page(self, url: str) -> list[str]:
        async with self.db.execute(
            "SELECT u.url FROM link_edges e JOIN urls u ON u.id = e.to_id "
            "WHERE e.from_id = (SELECT id FROM urls WHERE url = ?)",
            (url,),
        ) as cur:
            return [r["url"] for r in await cur.fetchall()]

    async def _explain_hydration(self) -> str:  # pragma: no cover — debug helper
        counts = await self.counts_by_status()
        return json.dumps(counts)


async def _demo() -> None:  # pragma: no cover — manual smoke
    db = StateDB(":memory:")
    await db.open()
    await db.save_meta({"seed": "https://x.test/", "depth": 3})
    await db.ensure_url("https://x.test/", 0)
    await db.ensure_url("https://x.test/a.html", 1, referer="https://x.test/")
    await db.mark_status("https://x.test/", "done", local_path="index.html",
                         content_hash="ab12", content_type="text/html")
    print(await db.get_pending())
    print(await db.counts_by_status())
    print(await db.load_meta())
    await db.close()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_demo())
