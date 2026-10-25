"""Phase 7 tests: headless-browser crawling and cookie auth.

Acceptance (build guide): a client-rendered page (content injected by JS
after load) is captured in headless mode but missed in plain-HTTP mode.
"""
import asyncio
import os

import pytest
from aiohttp import web

from app.core.crawler import Crawler
from app.core.headless import cookie_header, parse_cookie_header
from app.core.types import CrawlConfig

pytest.importorskip("playwright", reason="playwright not installed")

JS_PAGE = """<html><head><title>Plain</title></head><body>
<div id="x"></div>
<script>
  // client-side rendering: link + text only exist after JS runs
  document.getElementById('x').innerHTML =
    '<p>INJECTED-BY-JS wobble-content</p>' +
    '<a href="/late.html">late link</a>';
</script>
</body></html>"""


def build_site(requires_cookie: bool = False) -> web.Application:
    app = web.Application()

    def _authorized(request) -> bool:
        if not requires_cookie:
            return True
        return "session=secret" in request.headers.get("Cookie", "")

    async def index(request):
        if not _authorized(request):
            return web.Response(status=403, text="login required")
        return web.Response(
            text='<html><head><title>Home</title></head><body>'
                 '<a href="/js-page.html">js page</a></body></html>',
            content_type="text/html")

    async def js_page(request):
        if not _authorized(request):
            return web.Response(status=403, text="login required")
        return web.Response(text=JS_PAGE, content_type="text/html")

    async def late(request):
        return web.Response(text="<html><body>late page content</body></html>",
                            content_type="text/html")

    async def robots(request):
        return web.Response(text="User-agent: *\nAllow: /\n",
                            content_type="text/plain")

    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/", index)
    app.router.add_get("/js-page.html", js_page)
    app.router.add_get("/late.html", late)
    return app


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def _crawl(base: str, out: str, **kw) -> None:
    defaults = dict(
        seed=f"{base}/", output_dir=out, depth=2, concurrency=2,
        delay=0.0, retries=0, timeout=30.0,
        state_db=os.path.join(out, "project.db"),
    )
    defaults.update(kw)
    await Crawler(CrawlConfig(**defaults)).run()


def _read(out: str, rel: str) -> str:
    with open(os.path.join(out, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


async def test_plain_http_misses_js_content(tmp_path):
    runner, port = await start(build_site())
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "plain")
    try:
        await _crawl(base, out)
        body = _read(out, "js-page.html")
        # raw source: the injected link exists only inside the <script> string
        assert 'href="/late.html"' in body
        assert 'href="late.html"' not in body      # never rendered/rewritten
        assert not os.path.exists(os.path.join(out, "late.html"))
    finally:
        await runner.cleanup()


async def test_headless_captures_js_content(tmp_path):
    """The phase-7 acceptance criterion."""
    runner, port = await start(build_site())
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "headless")
    try:
        await _crawl(base, out, headless=True)
        body = _read(out, "js-page.html")
        assert "INJECTED-BY-JS" in body              # rendered DOM captured
        assert "wobble-content" in body
        assert "late.html" in body                    # the JS-injected link
        assert os.path.isfile(os.path.join(out, "late.html"))   # ...and crawled
        assert "late page content" in _read(out, "late.html")
    finally:
        await runner.cleanup()


async def test_cookies_open_gated_site(tmp_path):
    runner, port = await start(build_site(requires_cookie=True))
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "authed")
    try:
        await _crawl(base, out, cookies=("session=secret",))
        assert "INJECTED-BY-JS" in _read(out, "js-page.html")
    finally:
        await runner.cleanup()


async def test_missing_cookies_fail_gated_site(tmp_path):
    runner, port = await start(build_site(requires_cookie=True))
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "locked")
    try:
        report = await Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=2,
            delay=0.0, retries=0,
            state_db=os.path.join(out, "project.db"),
        )).run()
        assert report.stats.pages_failed >= 1          # 403s recorded as failures
        assert not os.path.exists(os.path.join(out, "js-page.html"))
    finally:
        await runner.cleanup()


def test_cookie_header_parsing():
    assert parse_cookie_header("a=1; b=2") == [("a", "1"), ("b", "2")]
    assert parse_cookie_header("junk;; c = 3 ") == [("c", "3")]
    assert cookie_header(("a=1", "b=2")) == "a=1; b=2"
