"""End-to-end integration test: full crawler -> downloader -> rewriter pipeline
against a local aiohttp site with a deliberate mix of features (build guide §10)
— relative/absolute links, CSS referencing fonts/images, a redirect, robots.txt
rules, a 404, and foreign-CDN assets.
"""
import asyncio
import json
import os
import re
import time

import pytest
from aiohttp import web

from app.core.crawler import Crawler
from app.core.rewriter import extract_html
from app.core.state_db import StateDB
from app.core.types import CrawlConfig

_ROBOTS = "User-agent: *\nDisallow: /secret/\n"


def _page(body: str) -> web.Response:
    return web.Response(text=body, content_type="text/html")


def build_site_app(ext_base: str = "https://cdn-assets.test",
                   hits: dict[str, int] | None = None) -> web.Application:
    middlewares = []
    if hits is not None:
        @web.middleware
        async def _count(request, handler):
            hits[request.path] = hits.get(request.path, 0) + 1
            return await handler(request)
        middlewares.append(_count)
    app = web.Application(middlewares=middlewares)

    async def robots(request):
        return web.Response(text=_ROBOTS, content_type="text/plain")
    app.router.add_get("/robots.txt", robots)

    async def index(request):
        return _page(
            "<html><head>"
            '<link rel="stylesheet" href="css/style.css">'
            '<link rel="icon" href="favicon.ico">'
            "</head><body>"
            '<img src="img/logo.png" srcset="img/logo.png 1x, img/logo@2x.png 2x">'
            '<script src="app.js"></script>'
            '<a href="page1.html">one</a>'
            '<a href="/page1.html">one absolute</a>'
            '<a href="old.html">legacy (redirects)</a>'
            '<a href="/secret/hidden.html">hidden</a>'
            '<a href="missing404.html">missing page</a>'
            '<a href="doc.pdf">download</a>'
            '<a href="' + ext_base + '/widget.js">cdn asset</a>'
            '<a href="https://foreign.test/page.html">foreign page</a>'
            '<a href="mailto:x@y.z">mail</a>'
            "<a href='#anchor'>anchor</a>"
            '<div style="background:url(img/inline.png)">inline css</div>'
            "</body></html>"
        )
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)

    async def style(request):
        return web.Response(
            text="body{background:url(../img/bg.png)}\n"
                 "@font-face{src:url(../fonts/f.woff2) format('woff2')}\n",
            content_type="text/css",
        )
    app.router.add_get("/css/style.css", style)

    async def favicon(request):
        return web.Response(body=b"ICON", content_type="image/x-icon")
    app.router.add_get("/favicon.ico", favicon)

    async def png(request):
        return web.Response(body=b"PNGIMAGE", content_type="image/png")
    app.router.add_get("/img/logo.png", png)
    app.router.add_get("/img/logo@2x.png", png)
    app.router.add_get("/img/bg.png", png)
    app.router.add_get("/img/inline.png", png)

    async def js(request):
        return web.Response(text="console.log('ok');", content_type="application/javascript")
    app.router.add_get("/app.js", js)

    async def font(request):
        return web.Response(body=b"WOFF", content_type="font/woff2")
    app.router.add_get("/fonts/f.woff2", font)

    async def page1(request):
        return _page(
            '<html><body><h1>One</h1>'
            '<a href="/">home</a><a href="page2.html">two</a>'
            '<img src="' + ext_base + '/remote.png">'
            "</body></html>"
        )
    app.router.add_get("/page1.html", page1)

    async def page2(request):
        return _page('<html><body><p>Two</p><img src="img/logo.png"></body></html>')
    app.router.add_get("/page2.html", page2)

    async def old(request):
        raise web.HTTPFound("/page2.html")
    app.router.add_get("/old.html", old)

    async def secret(request):
        return _page("<html><body>secret</body></html>")
    app.router.add_get("/secret/hidden.html", secret)

    async def missing(request):
        raise web.HTTPNotFound()
    app.router.add_get("/missing404.html", missing)

    async def pdf(request):
        return web.Response(body=b"%PDF-1.4 fake", content_type="application/pdf")
    app.router.add_get("/doc.pdf", pdf)

    return app


def build_cdn_app() -> web.Application:
    app = web.Application()

    async def widget(request):
        return web.Response(text="/*widget*/", content_type="application/javascript")
    app.router.add_get("/widget.js", widget)

    async def remote(request):
        return web.Response(body=b"REMOTEPNG", content_type="image/png")
    app.router.add_get("/remote.png", remote)

    return app


async def start_app(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


def rd(out: str, rel: str) -> str:
    with open(os.path.join(out, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


async def test_full_crawl_mirror(tmp_path):
    cdn_runner, cdn_port = await start_app(build_cdn_app())
    site_runner, site_port = await start_app(build_site_app(f"http://127.0.0.1:{cdn_port}"))
    base = f"http://127.0.0.1:{site_port}"
    out = str(tmp_path / "mirror")
    try:
        crawler = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, timeout=10.0, retries=0,
            respect_robots=True, verify_links=True,
        ))
        report = await crawler.run()
        s = report.stats

        # the 404 page fails, robots-blocked page is skipped (not a failure)
        assert s.pages_failed == 1, report.summary_text()
        assert s.skipped_robots == 1

        # ---------- saved page content
        index = rd(out, "index.html")
        assert 'href="page1.html"' in index
        assert 'href="css/style.css"' in index
        assert 'src="app.js"' in index
        # foreign page stays absolute; mailto untouched
        assert 'href="https://foreign.test/page.html"' in index
        assert "mailto:" in index
        # robots-blocked page left absolute
        assert f'href="{base}/secret/hidden.html"' in index
        # missing page stays absolute too
        assert f'href="{base}/missing404.html"' in index
        # redirected link resolves to the redirect TARGET file
        assert 'href="page2.html"' in index
        assert "old.html" not in [seg.split('"')[0] for seg in index.split('href="')[1:]]

        # ---------- css rewritten relative to its own dir
        css = rd(out, "css/style.css")
        assert "url(../img/bg.png)" in css                    # ../img from css dir
        assert "url(../fonts/f.woff2)" in css                # font file really there
        assert open(os.path.join(out, "img", "bg.png"), "rb").read() == b"PNGIMAGE"
        assert open(os.path.join(out, "img", "inline.png"), "rb").read() == b"PNGIMAGE"

        # ---------- foreign-host assets mirrored under their host folder
        # (Windows-safe: ':' in the netloc is sanitized to '_')
        p1 = rd(out, "page1.html")
        cdn_dir = f"127.0.0.1_{cdn_port}"
        assert f'src="{cdn_dir}/remote.png"' in p1
        assert open(os.path.join(out, cdn_dir, "remote.png"), "rb").read() == b"REMOTEPNG"

        # font exists
        assert open(os.path.join(out, "fonts", "f.woff2"), "rb").read() == b"WOFF"
        # doc.pdf saved (linked asset, not crawled)
        assert open(os.path.join(out, "doc.pdf"), "rb").read() == b"%PDF-1.4 fake"
        # foreign page NOT downloaded; foreign page link absolute (checked above)
        assert not os.path.exists(os.path.join(out, "foreign.test"))

        # ---------- manifest
        manifest = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
        urls = {r["url"] for r in manifest["resources"]}
        assert f"{base}/" in urls or f"{base}/index.html" in urls
        assert f"{base}/missing404.html" in {f["url"] for f in manifest["failures"]}
        assert manifest["redirects"].get(f"{base}/old.html") == f"{base}/page2.html"

        # ---------- offline link verification: only known-broken refs flagged
        broken = crawler.verify_mirror()
        for b in broken:
            assert b.reason in {"missing-file", "out-of-scope"}, b
        flagged = {b.target for b in broken}
        assert f"{base}/missing404.html" in flagged          # genuinely missing
    finally:
        await site_runner.cleanup()
        await cdn_runner.cleanup()


async def test_exclusions(tmp_path):
    runner, port = await start_app(build_site_app())
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "mirror2")
    try:
        crawler = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0,
            exclude=("*page2.html*",),
        ))
        report = await crawler.run()
        p1 = rd(out, "page1.html")
        assert f'href="{base}/page2.html"' in p1                  # left absolute
        assert not os.path.exists(os.path.join(out, "page2.html"))
        assert report.stats.skipped_excluded >= 1
    finally:
        await runner.cleanup()


async def test_depth_limits_crawl(tmp_path):
    runner, port = await start_app(build_site_app())
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "mirror3")
    try:
        crawler = Crawler(CrawlConfig(
            seed=f"{base}/page2.html", output_dir=out, depth=0,
            concurrency=4, per_host_concurrency=4, delay=0.0, retries=0,
        ))
        report = await crawler.run()
        manifest = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
        pages = {r["url"] for r in manifest["resources"] if r["kind"] == "page"}
        assert pages == {f"{base}/page2.html"}
        # seed != homepage -> entry-point redirect stub created
        stub = rd(out, "index.html")
        assert 'http-equiv="refresh"' in stub and "page2.html" in stub
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------
# file:// acceptance audit: resolve every relative ref in every saved page
# with pure-filesystem semantics (the way a browser would), independent of
# the engine's own URL machinery. Only intentionally-absolute refs may dangle.

_LOCAL_RE = re.compile(r"^(https?://|//|mailto:|javascript:|tel:|data:|about:|#)", re.I)


async def test_file_resolution_audit(tmp_path):
    runner, port = await start_app(build_site_app())
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "audit")
    try:
        crawler = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0,
        ))
        await crawler.run()

        broken = []
        for dirpath, _dirs, files in os.walk(out):
            for name in files:
                if not name.endswith(".html"):
                    continue
                page_rel = os.path.relpath(os.path.join(dirpath, name), out)
                text = open(os.path.join(dirpath, name), encoding="utf-8").read()
                ext = extract_html(text)
                refs = [r.url for r in ext.links] + [r.url for r in ext.assets]
                page_dir = os.path.dirname(page_rel)
                for u in refs:
                    if not u.strip() or _LOCAL_RE.match(u.strip()):
                        continue          # absolute / opaque: not a local ref
                    tgt = os.path.relpath(
                        os.path.normpath(os.path.join(out, page_dir,
                                                     u.replace("/", os.sep))),
                        out)
                    if not os.path.isfile(os.path.join(out, *tgt.split(os.sep))):
                        broken.append((page_rel, u, tgt))

        assert not broken, broken[:10]
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------
# phase 2: persistence + crash resume

def _mirror_files(out: str) -> dict[str, bytes]:
    """The actual result of a crawl: every mirrored file's relative path and
    bytes (manifest/state-db excluded — they are run bookkeeping)."""
    files: dict[str, bytes] = {}
    for dirpath, _dirs, names in os.walk(out):
        for name in names:
            if (name == "manifest.json" or name.startswith("project.db")
                    or name.startswith("search.db")):
                continue          # run bookkeeping, not mirror content
            rel = os.path.relpath(os.path.join(dirpath, name), out)
            with open(os.path.join(dirpath, name), "rb") as fh:
                files[rel] = fh.read()
    return files


async def test_kill_mid_crawl_then_resume(tmp_path):
    """Acceptance criterion (build guide phase 2): kill the process partway
    through, restart on the same output folder, and the resumed run must
    finish the job without re-downloading completed work."""
    hits: dict[str, int] = {}
    runner, port = await start_app(build_site_app(hits=hits))
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "mirror")
    reference_out = str(tmp_path / "reference")
    try:
        db_path = os.path.join(out, "project.db")

        # ---- run 1: simulate a crash while page2 is still "in flight"
        crawler = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0, state_db=db_path,
        ))
        gate = asyncio.Event()
        real_fetch = crawler.downloader.fetch

        async def gated_fetch(job, **kw):
            if job.url.endswith("/page2.html"):
                await gate.wait()          # pretend the server hung
            return await real_fetch(job, **kw)

        crawler.downloader.fetch = gated_fetch
        task = asyncio.create_task(crawler.run())
        # wait until at least one row is durably 'done' (WAL allows this
        # concurrent read), so the kill always leaves a meaningful partial
        # state — not the pre-commit window of the very first page
        probe = StateDB(db_path)
        await probe.open()
        try:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                counts = await probe.counts_by_status()
                if counts.get("done", 0) >= 1:
                    break
                await asyncio.sleep(0.005)
        finally:
            await probe.close()
        await asyncio.sleep(0.02)     # let the frontier advance a little more
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()

        # journal state after the kill: some rows done, page2 unfinished
        db = StateDB(db_path)
        await db.open()
        counts = await db.counts_by_status()
        assert counts.get("done", 0) >= 1
        assert counts.get("pending", 0) >= 1
        assert counts.get("downloading", 0) + counts.get("pending", 0) >= 1
        done_before = {row["url"] for row in await db.hydrate_records()}
        assert done_before, "kill landed before anything completed"
        await db.close()
        hits_after_kill = dict(hits)

        # ---- run 2: resume with a fresh process object, same output + db
        crawler2 = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0, state_db=db_path,
        ))
        report2 = await crawler2.run()
        assert report2.resumed_done >= 1
        assert report2.resumed_pending >= 1

        # pages finished before the kill were NOT fetched again
        for url in done_before:
            path = "/" + url.split(f":{port}/", 1)[-1].split("?", 1)[0]
            if path in ("/robots.txt", "/"):
                continue
            if hits_after_kill.get(path) == 1:
                assert hits.get(path) == 1, f"{path} was re-downloaded on resume"

        # ---- reference: one uninterrupted run of the same site
        ref = Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=reference_out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0,
        ))
        await ref.run()

        assert _mirror_files(out) == _mirror_files(reference_out)

        # the resumed mirror passes the same file:// audit as the reference
        for broken in (crawler2.verify_mirror(),):
            for b in broken:
                assert b.reason in {"missing-file", "out-of-scope"}, b
    finally:
        await runner.cleanup()


async def test_resume_finished_project_downloads_nothing(tmp_path):
    """Re-running a completed project must not re-download anything."""
    hits: dict[str, int] = {}
    runner, port = await start_app(build_site_app(hits=hits))
    base = f"http://127.0.0.1:{port}"
    out = str(tmp_path / "mirror")
    try:
        cfg = dict(
            seed=f"{base}/", output_dir=out, depth=2, concurrency=4,
            per_host_concurrency=4, delay=0.0, retries=0,
            state_db=os.path.join(out, "project.db"),
        )
        first = Crawler(CrawlConfig(**cfg))
        await first.run()
        hits_after_first = dict(hits)

        # regression: every page/asset must be fetched exactly once —
        # /page2.html may legitimately be 2 (redirect target of old.html
        # plus its own job, interleaving-dependent)
        for path, n in hits.items():
            if path == "/robots.txt":
                continue
            assert n == 1 or (path == "/page2.html" and n == 2), \
                f"{path} fetched {n} times"

        second = Crawler(CrawlConfig(**cfg))
        report = await second.run()
        assert report.resumed_pending == 0          # nothing left to redo
        assert report.resumed_done > 0             # whole previous run kept

        # only robots.txt (fresh cache per process) may be requested again
        for path, n in hits.items():
            if path == "/robots.txt":
                continue
            assert n == hits_after_first.get(path, n), f"{path} re-downloaded"
    finally:
        await runner.cleanup()
