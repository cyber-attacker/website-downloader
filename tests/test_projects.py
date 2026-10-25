"""Phase 5 tests for the project store (GUI-agnostic)."""
import asyncio
import json
import os
import sqlite3

import pytest
from aiohttp import web

from app.core.crawler import Crawler
from app.core.projects import config_for, read_summary, scan_projects
from app.core.types import CrawlConfig


def build_site(n_pages: int = 6) -> web.Application:
    app = web.Application()

    async def index(request):
        links = "".join(f'<a href="/p{i}.html">p{i}</a>' for i in range(n_pages))
        return web.Response(text=f"<html><body>{links}</body></html>",
                            content_type="text/html")

    async def page(request):
        return web.Response(text="<html><body>page</body></html>",
                            content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/{tail:.*}", page)
    return app


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def run_crawl(base: str, out: str, **kw) -> None:
    asyncio.run(_crawl(base, out, **kw))


async def _crawl(base: str, out: str, **kw):
    runner, port = await start(build_site())
    try:
        defaults = dict(
            seed=f"http://127.0.0.1:{port}/", output_dir=out, depth=1,
            concurrency=4, delay=0.0, retries=0,
            state_db=os.path.join(out, "project.db"),
        )
        defaults.update(kw)
        await Crawler(CrawlConfig(**defaults)).run()
    finally:
        await runner.cleanup()


def test_complete_project_scanned(tmp_path):
    root = tmp_path / "projects"
    proj = root / "site-a"
    proj.mkdir(parents=True)
    run_crawl(str(tmp_path), str(proj))

    summaries = scan_projects(str(root))
    assert len(summaries) == 1
    s = summaries[0]
    assert s.name == "site-a"
    assert s.status == "complete"
    assert s.progress_pct == 100
    assert s.seed.startswith("http://127.0.0.1:")
    assert s.depth == 1
    assert s.pages_done == 7                       # index + 6 pages
    assert s.last_run_at is not None


def test_resumable_project_detected(tmp_path):
    root = tmp_path / "projects"
    proj = root / "site-b"
    proj.mkdir(parents=True)
    run_crawl(str(tmp_path), str(proj))
    # simulate a kill: flip two done rows back to pending
    db = os.path.join(proj, "project.db")
    con = sqlite3.connect(db)
    con.execute("UPDATE urls SET status='pending' "
                "WHERE url LIKE '%p5.html' OR url LIKE '%p4.html'")
    con.commit()
    con.close()

    s = read_summary(str(proj))
    assert s.status == "incomplete"
    assert 0 < s.progress_pct < 100
    assert s.pages_done == 5


def test_config_for_rebuilds_original_settings(tmp_path):
    root = tmp_path / "projects"
    proj = root / "site-c"
    proj.mkdir(parents=True)
    run_crawl(str(tmp_path), str(proj), exclude=("*p3*",), depth=1)

    s = read_summary(str(proj))
    cfg = config_for(s)
    assert cfg.output_dir == str(proj)
    assert cfg.depth == 1
    assert cfg.exclude == ("*p3*",)
    assert cfg.state_db == os.path.join(proj, "project.db")
    assert cfg.seed == s.seed


def test_scan_ignores_folders_without_db(tmp_path):
    root = tmp_path / "projects"
    (root / "not-a-project").mkdir(parents=True)
    (root / "empty-project").mkdir(parents=True)
    # a project.db with no rows at all
    dbdir = root / "bare"
    dbdir.mkdir(parents=True)
    con = sqlite3.connect(str(dbdir / "project.db"))
    con.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, status TEXT, "
                "depth INTEGER, content_type TEXT)")
    con.execute("CREATE TABLE project_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()

    summaries = scan_projects(str(root))
    assert [s.name for s in summaries] == ["bare"]
    assert summaries[0].status == "empty"
