"""Phase 6 tests: preview server + FTS5 search index."""
import asyncio
import json
import os

import aiohttp
import pytest
from aiohttp import web

from app.core.crawler import Crawler
from app.core.preview_server import PreviewServer
from app.core.search_index import SearchIndex, extract_text, open_ro
from app.core.types import CrawlConfig


def build_site() -> web.Application:
    """Pages with distinctive words so FTS queries are unambiguous."""
    app = web.Application()

    async def index(request):
        return web.Response(
            text='<html><head><title>Home</title>'
                 '<link rel="stylesheet" href="/theme.css"></head><body>'
                 '<a href="/deployment.html">deployment guide</a>'
                 '<a href="/kittens.html">kittens</a>'
                 '</body></html>',
            content_type="text/html")

    async def deployment(request):
        return web.Response(
            text="<html><head><title>Deployment guide</title></head>"
                 "<body>how to deploy the wobble service step by step</body></html>",
            content_type="text/html")

    async def kittens(request):
        return web.Response(
            text="<html><head><title>Kittens</title></head>"
                 "<body>adorable kittens purr loudly</body></html>",
            content_type="text/html")

    async def style(request):
        return web.Response(text="body{color:red}", content_type="text/css")

    app.router.add_get("/", index)
    app.router.add_get("/deployment.html", deployment)
    app.router.add_get("/kittens.html", kittens)
    app.router.add_get("/theme.css", style)
    return app


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def _crawl(out: str, **kw) -> None:
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


async def test_search_index_built_and_queried(tmp_path):
    out = str(tmp_path / "mirror")
    await _crawl(out)

    db = os.path.join(out, "search.db")
    assert os.path.isfile(db)

    index = open_ro(db)
    try:
        hits = index.search("wobble")
        assert len(hits) == 1
        assert hits[0].path == "deployment.html"
        assert hits[0].title == "Deployment guide"
        assert "wobble" in hits[0].snippet

        assert index.search("kittens")[0].path == "kittens.html"
        assert index.search("purring") == []            # porter stems, not fuzzy
        assert index.search('"purr loudly"')            # phrase query works
        assert index.search("NOT A QUERY [") == []      # malformed never crashes
        assert index.search("") == []
    finally:
        index.close()


async def test_resume_does_not_duplicate_index(tmp_path):
    out = str(tmp_path / "mirror")
    await _crawl(out)
    db = os.path.join(out, "search.db")

    def count():
        import sqlite3
        con = sqlite3.connect(db)
        n = con.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]
        con.close()
        return n

    n1 = count()
    assert n1 >= 3                                        # home + 2 pages
    # a resume run re-finalizes + re-indexes: same rows, no duplicates
    await _crawl(out)
    assert count() == n1


async def test_no_search_when_disabled(tmp_path):
    out = str(tmp_path / "mirror")
    await _crawl(out, build_search_index=False)
    assert not os.path.exists(os.path.join(out, "search.db"))


def test_extract_text_drops_scripts_and_styles():
    html = ("<html><head><title>T</title><style>body{}</style></head>"
            "<body><script>var x=1;</script><h1>Visible words</h1></body></html>")
    title, text = extract_text(html)
    assert title == "T"
    assert "Visible words" in text and "var" not in text


# ------------------------------------------------------------- preview server

async def test_preview_server_serves_mirror(tmp_path):
    out = str(tmp_path / "mirror")
    await _crawl(out)

    server = PreviewServer(out)
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/") as resp:
                assert resp.status == 200
                assert "text/html" in resp.headers["Content-Type"]
                body = await resp.text()
                assert "deployment guide" in body      # the rewritten page
            # proper MIME for css — the thing file:// gets wrong
            async with s.get(f"{base}/theme.css") as resp:
                assert resp.status == 200
                assert "text/css" in resp.headers["Content-Type"]
            async with s.get(f"{base}/missing-page.html") as resp:
                assert resp.status == 404
            # directory-style page URL resolves to its index.html
            async with s.get(f"{base}/deployment.html") as resp:
                assert resp.status == 200
                assert "wobble" in await resp.text()
    finally:
        await server.stop()

    # after stop the port is closed (refused or timeout — never a 200)
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(f"{base}/", timeout=aiohttp.ClientTimeout(total=2)):
                pytest.fail("server still serving after stop()")
        except (aiohttp.ClientError, TimeoutError, OSError):
            pass
