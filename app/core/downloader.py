"""Async HTTP fetching: connection pool, retries with backoff, redirect
tracking, size caps, per-host politeness."""
from __future__ import annotations

import asyncio
import random
import re
import time
from urllib.parse import urlsplit

import aiohttp

from app.core.types import CrawlConfig, FetchResult, Job
from app.utils.robots import RobotsCache

RETRYABLE_STATUS = {500, 502, 503, 504}
RATE_LIMITED_STATUS = {429}

_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", re.I)


def sniff_charset(body: bytes, header_charset: str | None) -> str:
    """Content-Type charset wins; else a <meta charset=...> sniff; else utf-8."""
    if header_charset:
        c = header_charset.strip().lower()
        if c and c != "none":
            return c
    m = _META_CHARSET_RE.search(body[:4096])
    if m:
        return m.group(1).decode("ascii", "replace").lower()
    return "utf-8"


class Downloader:
    """All HTTP work goes through here — the GUI will reuse it unchanged."""

    def __init__(self, config: CrawlConfig, robots: RobotsCache | None = None) -> None:
        self.config = config
        self.robots = robots
        self._session: aiohttp.ClientSession | None = None
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self.on_redirect = None            # callback(src_url, final_url) for the crawler
        self.proxy_manager = None
        if config.proxies:
            from app.core.proxy_manager import ProxyManager

            self.proxy_manager = ProxyManager(
                config.proxies,
                pin_domains=config.pin_domain_proxy,
                check_url=config.proxy_check_url,
            )
        self._socks_sessions: dict[str, aiohttp.ClientSession] = {}

    # ---- lifecycle -------------------------------------------------------

    def create_session(self) -> None:
        cfg = self.config
        connector = aiohttp.TCPConnector(
            limit=cfg.concurrency,
            limit_per_host=max(cfg.per_host_concurrency, 1),
            ttl_dns_cache=120,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers=self._default_headers(),
            auto_decompress=True,
        )

    def _default_headers(self) -> dict:
        cfg = self.config
        headers = {
            "User-Agent": cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cfg.cookies:
            from app.core.headless import cookie_header
            headers["Cookie"] = cookie_header(cfg.cookies)
        return headers

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self.create_session()
        return self._session

    def session_factory(self):
        # used by RobotsCache
        return self.session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        for session in self._socks_sessions.values():
            if not session.closed:
                await session.close()
        self._socks_sessions.clear()

    # ---- politeness ------------------------------------------------------

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    def set_crawl_delay(self, url: str, seconds: float) -> None:
        host = (urlsplit(url).hostname or "").lower()
        cur = self._host_last.get("delay:" + host, 0.0)
        if seconds > cur:
            self._host_last["delay:" + host] = seconds

    def _delay_for(self, host: str) -> float:
        return self._host_last.get("delay:" + host, 0.0)

    async def wait_turn(self, url: str, crawl_delay: float = 0.0) -> None:
        """Serialize requests to the same host honoring configured/robots delay."""
        host = (urlsplit(url).hostname or "").lower()
        gap = max(self.config.delay, crawl_delay, self._delay_for(host))
        if gap <= 0:
            return
        async with self._lock_for(host):
            last = self._host_last.get(host, 0.0)
            wait = last + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    # ---- fetching --------------------------------------------------------

    async def setup_proxies(self) -> None:
        """Health-check the pool once per run (called by Crawler.run())."""
        if self.proxy_manager is not None and not self.proxy_manager.health_checked:
            await self.proxy_manager.health_check()

    def _request_session(self, proxy) -> tuple[aiohttp.ClientSession, dict]:
        """Session + kwargs for one request, honoring a (possibly SOCKS) proxy."""
        if proxy is not None and proxy.scheme == "socks5":
            session = self._socks_sessions.get(proxy.key)
            if session is None or session.closed:
                connector = proxy.connector()
                session = aiohttp.ClientSession(connector=connector)
                self._socks_sessions[proxy.key] = session
            return session, {}
        kwargs: dict = {}
        if proxy is not None:
            kwargs["proxy"] = proxy.aios_url
        return self.session, kwargs

    async def fetch(self, job: Job, *, respect_robots: bool = True) -> FetchResult:
        cfg = self.config
        url = job.url
        crawl_delay = 0.0
        domain = urlsplit(url).hostname or ""

        if respect_robots and self.robots and cfg.respect_robots:
            try:
                allowed, robots_delay = await self.robots.allowed(url, cfg.user_agent)
                crawl_delay = robots_delay
            except Exception:
                allowed = True
            if not allowed:
                return FetchResult(ok=False, url=url, error="robots.txt disallowed", attempts=0)

        if cfg.delay or crawl_delay:
            await self.wait_turn(url, crawl_delay)

        proxy = None
        if self.proxy_manager is not None:
            proxy = await self.proxy_manager.acquire(domain)

        attempts = 0
        backoff = cfg.backoff_base
        last_error: str | None = None
        last_status: int | None = None

        while attempts <= cfg.retries:
            attempts += 1
            session, req_kwargs = self._request_session(proxy)
            headers = {}
            if self.proxy_manager is not None and cfg.rotate_user_agent:
                ua = self.proxy_manager.next_ua()
                if ua:
                    headers["User-Agent"] = ua
            try:
                timeout = aiohttp.ClientTimeout(total=cfg.timeout, sock_connect=min(cfg.timeout, 15.0))
                async with session.get(url, timeout=timeout, allow_redirects=True,
                                       max_redirects=10, headers=headers or None,
                                       **req_kwargs) as resp:
                    last_status = resp.status
                    redirect_from = None
                    if resp.history and resp.history[0]:
                        redirect_from = str(resp.history[0].url)
                        if self.on_redirect and str(resp.url) != redirect_from:
                            try:
                                self.on_redirect(redirect_from, str(resp.url))
                            except Exception:
                                pass
                    if resp.content_length and resp.content_length > cfg.max_file_size:
                        return FetchResult(
                            ok=False, url=url, final_url=str(resp.url),
                            redirect_from=redirect_from, status=resp.status,
                            error=f"content-length {resp.content_length} exceeds max size",
                            too_large=True, attempts=attempts,
                        )
                    data = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > cfg.max_file_size:
                            return FetchResult(
                                ok=False, url=url, final_url=str(resp.url),
                                redirect_from=redirect_from, status=resp.status,
                                error=f"stream exceeds max size ({cfg.max_file_size} B)",
                                too_large=True, attempts=attempts,
                            )
                    if resp.status == 200:
                        return FetchResult(
                            ok=True, url=url, final_url=str(resp.url),
                            redirect_from=redirect_from, status=resp.status,
                            content_type=resp.headers.get("Content-Type", ""),
                            charset=resp.charset,
                            etag=resp.headers.get("ETag"),
                            last_modified=resp.headers.get("Last-Modified"),
                            data=bytes(data), attempts=attempts,
                        )
                    if resp.status in RETRYABLE_STATUS or resp.status in RATE_LIMITED_STATUS:
                        last_error = f"HTTP {resp.status}"
                    elif resp.status == 403 and proxy is not None:
                        # a proxy that got itself blocked: rotate and retry
                        # elsewhere before this URL is marked failed
                        last_error = "HTTP 403 via proxy"
                    else:
                        return FetchResult(
                            ok=False, url=url, final_url=str(resp.url),
                            redirect_from=redirect_from, status=resp.status,
                            attempts=attempts, error=f"HTTP {resp.status}",
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if proxy is not None:
                    # connection reset / refused through this proxy — rotate
                    last_error += " (via proxy)"
            if attempts <= cfg.retries:
                if proxy is not None and last_error and (
                    "via proxy" in last_error
                    or "403 via proxy" in last_error
                    or "HTTP 429" in last_error
                ):
                    self.proxy_manager.report_failure(proxy, last_error)
                    proxy = await self.proxy_manager.acquire(domain)
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))
                backoff *= 2.0
        if proxy is not None and last_error and "via proxy" in last_error:
            self.proxy_manager.report_failure(proxy, last_error)
        return FetchResult(
            ok=False, url=url, status=last_status, attempts=attempts,
            error=last_error or "exhausted retries",
        )
