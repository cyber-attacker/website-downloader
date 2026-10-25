"""Full-text search over the mirrored pages (SQLite FTS5).

The index lives at <output>/search.db next to project.db. It is rebuilt from
the finalize pass — every saved page is re-read there anyway, so indexing
adds no extra crawl traffic and stays correct across resumes (rows are
keyed by URL and replaced).
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from app.core.rewriter import HTMLParser, extract_style_urls  # noqa: F401 (re-export clarity)


@dataclass
class SearchHit:
    url: str
    path: str
    title: str
    snippet: str


def extract_text(html: str) -> tuple[str, str]:
    """Plain text + title from a saved page (scripts/styles dropped)."""
    try:
        from selectolax.lexbor import LexborHTMLParser as HTMLParser
    except ImportError:                      # pragma: no cover
        from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    for tag in ("script", "style", "noscript"):
        for node in tree.css(tag):
            node.decompose()
    title_node = tree.css_first("title")
    title = (title_node.text(strip=True) if title_node is not None else "") or ""
    body_node = tree.css_first("body") or tree.root
    text = (body_node.text(separator=" ", strip=True) if body_node is not None else "")
    return title, " ".join(text.split())


def _connect_rw(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: the finalize pass drives this via
    # asyncio.to_thread, so calls may hop executor threads — but they are
    # strictly serialized (each is awaited), never concurrent.
    con = sqlite3.connect(db_path, timeout=10.0, check_same_thread=False)
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5("
                "url UNINDEXED, path UNINDEXED, title, body, "
                "tokenize='porter unicode61')")
    con.commit()
    return con


class SearchIndex:
    """Synchronous on purpose: the finalize pass calls it from a worker
    thread (asyncio.to_thread) and GUI queries are sub-millisecond."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.con = _connect_rw(db_path)

    # ---- writing ------------------------------------------------------------

    def add_page(self, url: str, path: str, html: str) -> None:
        title, body = extract_text(html)
        if not body:
            return
        cur = self.con.cursor()
        cur.execute("DELETE FROM pages_fts WHERE url = ?", (url,))
        cur.execute("INSERT INTO pages_fts(url, path, title, body) VALUES(?,?,?,?)",
                    (url, path, title, body[:200_000]))
        self.con.commit()

    def remove_url(self, url: str) -> None:
        self.con.execute("DELETE FROM pages_fts WHERE url = ?", (url,))
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    # ---- querying -------------------------------------------------------------

    def search(self, query: str, limit: int = 25) -> list[SearchHit]:
        if not query.strip():
            return []
        try:
            rows = self.con.execute(
                "SELECT url, path, title, "
                "snippet(pages_fts, 3, '[', ']', ' … ', 14) "
                "FROM pages_fts WHERE pages_fts MATCH ? "
                "ORDER BY bm25(pages_fts, 3.0, 1.0) LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []                    # malformed MATCH syntax etc.
        return [SearchHit(url=r[0], path=r[1], title=r[2] or r[0], snippet=r[3])
                for r in rows]


def open_ro(db_path: str) -> SearchIndex:
    """Read-only-ish handle for GUI queries (same class; the GUI only calls
    search())."""
    return SearchIndex(db_path)
