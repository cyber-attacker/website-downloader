"""Phase 3 tests: proxy rotation, health checks, UA rotation, retry-elsewhere.

The "mock proxies" are aiohttp servers behaving like tiny forward proxies:
health-check requests get a local 204; absolute-form GETs are relayed
server-side so the client really sees the target's response.
"""
import aiohttp
import pytest
from aiohttp import web

from app.core.downloader import Downloader
from app.core.proxy_manager import ProxyManager, UA_POOL
from app.core.types import CrawlConfig, Job

CHECK_MARKER = "generate_204"


def build_proxy_app(mode: str = "ok"):
    """mode: ok (relay everything) | flaky (403 for relays, 204 for checks)."""
    app = web.Application()

    async def handler(request):
        raw = request.raw_path                # absolute URI for proxy-style requests
        if CHECK_MARKER in raw:
            return web.Response(status=204)
        if raw.startswith("http://") or raw.startswith("https://"):
            if mode == "flaky":
                return web.Response(status=403, text="blocked by flaky proxy")
            # relay like a real forward proxy would
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                fwd = {"User-Agent": request.headers.get("User-Agent", "")}
                async with aiohttp.ClientSession(timeout=timeout) as s:
                    async with s.get(raw, headers=fwd) as resp:
                        body = await resp.read()
                        return web.Response(status=resp.status, body=body,
                                            content_type=resp.content_type)
            except Exception:
                return web.Response(status=502, text="relay failed")
        return web.Response(status=200, text="direct request")

    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def build_target_app(kind: str = "page"):
    app = web.Application()

    async def handler(request):
        if kind == "echo_ua":
            return web.Response(text=request.headers.get("User-Agent", ""))
        if kind == "block":
            return web.Response(status=403, text="forbidden")
        return web.Response(text="<html><body>target page</body></html>",
                            content_type="text/html")

    app.router.add_route("*", "/{tail:.*}", handler)
    return app


async def start(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


@pytest.fixture()
async def proxy_farm():
    runners = {}
    out = {}
    for name, mode in (("a", "ok"), ("b", "ok"), ("flaky", "flaky")):
        runners[name], port = await start(build_proxy_app(mode))
        out[name] = f"http://127.0.0.1:{port}"
    yield out
    for r in runners.values():
        await r.cleanup()


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


async def test_health_check_skips_dead_proxies(proxy_farm):
    dead_runner, dead_port = await start(web.Application())
    await dead_runner.cleanup()           # port is now closed -> connect refused
    mgr = ProxyManager(
        (proxy_farm["a"], f"http://127.0.0.1:{dead_port}", proxy_farm["b"]),
        check_url="http://x.test/generate_204",
    )
    await mgr.health_check()
    alive_ports = {p.port for p in mgr.proxies if p.alive}
    dead = [p for p in mgr.proxies if not p.alive]
    assert dead and dead[0].port == dead_port
    assert _port(proxy_farm["a"]) in alive_ports
    assert _port(proxy_farm["b"]) in alive_ports
    assert len(alive_ports) == 2


async def test_round_robin_and_domain_pinning(proxy_farm):
    mgr = ProxyManager((proxy_farm["a"], proxy_farm["b"]))
    await mgr.health_check()
    first = await mgr.acquire("example.com")
    second = await mgr.acquire("example.com")
    assert first is not second            # round-robin alternates

    pinned = ProxyManager((proxy_farm["a"], proxy_farm["b"]), pin_domains=True)
    await pinned.health_check()
    p1 = await pinned.acquire("site.test")
    p2 = await pinned.acquire("site.test")
    assert p1 is p2                        # same domain -> same proxy


async def test_failure_cooldown_yields_different_proxy(proxy_farm):
    mgr = ProxyManager((proxy_farm["a"], proxy_farm["b"]))
    await mgr.health_check()
    p1 = await mgr.acquire("site.test")
    mgr.report_failure(p1, "HTTP 403 via proxy")
    p2 = await mgr.acquire("site.test")
    assert p2 is not p1                   # cooled down -> different proxy
    assert not p1.available()


async def test_ua_rotation_cycles():
    mgr = ProxyManager(())
    uas = {mgr.next_ua() for _ in range(len(UA_POOL) * 2)}
    assert uas == set(UA_POOL)


async def test_downloader_retries_403_through_different_proxy(proxy_farm):
    """Acceptance (phase 3): a 403 through one proxy must retry through a
    different proxy instead of failing the URL."""
    target_runner, target_port = await start(build_target_app())
    target = f"http://127.0.0.1:{target_port}/page.html"
    try:
        cfg = CrawlConfig(
            seed=target, output_dir="unused", retries=1, delay=0.0,
            proxies=(proxy_farm["flaky"], proxy_farm["a"]),
            proxy_check_url="http://x.test/generate_204",
        )
        dl = Downloader(cfg)
        await dl.setup_proxies()
        assert dl.proxy_manager.alive_count == 2

        res = await dl.fetch(Job(url=target, depth=0))
        assert res.ok, res.error           # 403 via flaky -> rotated -> a -> 200
        flaky = [p for p in dl.proxy_manager.proxies
                 if p.port == _port(proxy_farm["flaky"])]
        assert flaky[0].failure_count == 1  # the block was blamed on the proxy
        await dl.close()
    finally:
        await target_runner.cleanup()


async def test_downloader_without_proxies_fails_403_immediately():
    runner, port = await start(build_target_app("block"))
    target = f"http://127.0.0.1:{port}/page.html"
    try:
        cfg = CrawlConfig(seed=target, output_dir="unused", retries=0, delay=0.0)
        dl = Downloader(cfg)
        res = await dl.fetch(Job(url=target, depth=0))
        assert not res.ok and "403" in (res.error or "")
        await dl.close()
    finally:
        await runner.cleanup()


async def test_ua_rotation_reaches_the_wire(proxy_farm):
    runner, port = await start(build_target_app("echo_ua"))
    target = f"http://127.0.0.1:{port}/ua.html"
    dl = None
    try:
        cfg = CrawlConfig(
            seed=target, output_dir="unused", retries=0, delay=0.0,
            proxies=(proxy_farm["a"],), rotate_user_agent=True,
            proxy_check_url="http://x.test/generate_204",
        )
        dl = Downloader(cfg)
        await dl.setup_proxies()
        seen = set()
        for _ in range(len(UA_POOL)):     # one full rotation cycle
            res = await dl.fetch(Job(url=target, depth=0))
            assert res.ok
            seen.add(res.data.decode())
        assert seen == set(UA_POOL)        # every request carried a rotated UA
    finally:
        if dl is not None:
            await dl.close()
        await runner.cleanup()


async def test_parse_rejects_bad_proxies():
    with pytest.raises(ValueError):
        ProxyManager(("ftp://x:21",))
    mgr = ProxyManager(("127.0.0.1:8080",))
    assert mgr.proxies[0].scheme == "http"   # bare host:port defaults to http
    assert mgr.proxies[0].port == 8080
