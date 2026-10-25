"""Unit tests for the persistent state database (phase 2)."""
import os
import time

import pytest

from app.core.state_db import StateDB, content_hash_of


@pytest.fixture()
async def db(tmp_path):
    db = StateDB(os.path.join(tmp_path, "project.db"))
    await db.open()
    yield db
    await db.close()


async def test_schema_and_meta_roundtrip(db):
    await db.save_meta({"seed_url": "https://x.test/", "depth": 3})
    await db.save_meta({"depth": 5})                 # upsert, not duplicate
    meta = await db.load_meta()
    assert meta["seed_url"] == "https://x.test/"
    assert meta["depth"] == 5


async def test_ensure_url_dedupes_and_returns_id(db):
    a = await db.ensure_url("https://x.test/a.html", 1)
    b = await db.ensure_url("https://x.test/a.html", 2)
    assert a == b > 0                                # same row, no duplicate
    other = await db.ensure_url("https://x.test/b.html", 1)
    assert other != a


async def test_status_lifecycle(db):
    await db.ensure_url("https://x.test/a.html", 1)
    await db.mark_status("https://x.test/a.html", "downloading")
    await db.mark_status("https://x.test/a.html", "done",
                         local_path="a.html", content_hash="ab12",
                         content_type="text/html", etag='"v1"',
                         last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
    rows = await db.hydrate_records()
    assert len(rows) == 1
    row = rows[0]
    assert row["local_path"] == "a.html" and row["content_hash"] == "ab12"
    assert row["etag"] == '"v1"'
    assert row["downloaded_at"] is not None
    assert await db.get_pending() == []               # done rows are not requeued


async def test_pending_includes_downloading_crash_state(db):
    await db.ensure_url("https://x.test/a.html", 1)
    await db.mark_status("https://x.test/a.html", "downloading")
    pending = await db.get_pending()
    assert [p.url for p in pending] == ["https://x.test/a.html"]
    assert pending[0].depth == 1


async def test_record_failure_retry_then_failed(db):
    await db.ensure_url("https://x.test/x.html", 1)
    s = await db.record_failure("https://x.test/x.html", "HTTP 500", 1, max_retries=3)
    assert s == "pending"                            # budget left -> retried
    s = await db.record_failure("https://x.test/x.html", "HTTP 500", 3, max_retries=3)
    assert s == "failed"                             # exhausted
    assert "https://x.test/x.html" in await db.skipped_or_failed_urls()


async def test_record_skip_and_never_overwrites_done(db):
    await db.ensure_url("https://x.test/a.html", 1)
    await db.mark_status("https://x.test/a.html", "done", local_path="a.html")
    await db.record_skip("https://x.test/a.html", 1, "excluded by rules")
    rows = await db.hydrate_records()
    assert len(rows) == 1                            # done stays done


async def test_skip_upsert_on_pending_row(db):
    await db.ensure_url("https://x.test/a.html", 1)
    await db.record_skip("https://x.test/a.html", 1, "robots.txt disallowed")
    assert await db.get_pending() == []
    assert await db.skipped_or_failed_urls() == {"https://x.test/a.html"}


async def test_skip_overwrites_stale_downloading_row(db):
    # a killed process can leave a row stuck in the transient 'downloading'
    # state; a later skip decision must still win over it
    await db.ensure_url("https://x.test/a.html", 1)
    await db.mark_status("https://x.test/a.html", "downloading")
    await db.record_skip("https://x.test/a.html", 1, "robots.txt disallowed")
    assert await db.skipped_or_failed_urls() == {"https://x.test/a.html"}


async def test_known_urls_and_counts(db):
    await db.ensure_url("https://x.test/", 0)
    await db.ensure_url("https://x.test/a.html", 1)
    await db.record_skip("https://x.test/b.html", 2, "beyond max depth 1")
    assert await db.all_known_urls() == {
        "https://x.test/", "https://x.test/a.html", "https://x.test/b.html"}
    assert await db.counts_by_status() == {"pending": 2, "skipped": 1}


async def test_link_edges(db):
    seed = await db.ensure_url("https://x.test/", 0)
    child = await db.ensure_url("https://x.test/a.html", 1, referer="https://x.test/")
    assert child > 0 and seed > 0
    assert await db.edges_for_page("https://x.test/") == ["https://x.test/a.html"]
    # repeated discovery must not duplicate the edge
    await db.ensure_url("https://x.test/a.html", 1, referer="https://x.test/")
    assert len(await db.edges_for_page("https://x.test/")) == 1


async def test_wal_and_repeat_open(db, tmp_path):
    """Reopening an existing database file must keep all committed state."""
    await db.ensure_url("https://x.test/a.html", 1)
    await db.mark_status("https://x.test/a.html", "done", local_path="a.html")
    await db.close()
    db2 = StateDB(os.path.join(tmp_path, "project.db"))
    await db2.open()
    assert len(await db2.get_pending()) == 0
    assert len(await db2.hydrate_records()) == 1
    await db2.close()


def test_content_hash_stable():
    assert content_hash_of(b"data") == content_hash_of(b"data")
    assert content_hash_of(b"data") != content_hash_of(b"other")
