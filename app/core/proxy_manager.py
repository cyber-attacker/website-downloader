"""Rotating, health-checked proxy pool with per-domain pinning and UA rotation.

Design (build guide, phase 3):
- accepts HTTP/HTTPS/SOCKS5 proxy URLs ("http://user:pass@host:port")
- health-checks every proxy once at startup with one lightweight request;
  dead ones are marked skipped and never handed out
- hands proxies out round-robin; with *pin_domains* the same domain always
  gets the same proxy (sticky routing)
- report_failure(proxy) → the proxy goes to a short cooldown and the caller
  gets a *different* one, so a 403/429/connection-reset retries elsewhere
  before the URL is marked failed
- user agents rotate per request out of a small built-in pool
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

try:                                    # optional: SOCKS5 support
    from aiohttp_socks import ProxyConnector
    HAVE_AIOHTTP_SOCKS = True
except ImportError:                     # pragma: no cover
    ProxyConnector = None               # type: ignore[assignment,misc]
    HAVE_AIOHTTP_SOCKS = False

# a handful of realistic, rotating identities (Chrome/Firefox/Edge on the
# three big OSes) — combined with the project UA when rotation is on
UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
)

COOLDOWN_S = 15.0        # a failed proxy waits this long before re-entering rotation


@dataclass
class Proxy:
    raw: str                      # the original URL, as the user wrote it
    scheme: str = "http"
    host: str = ""
    port: int = 0
    alive: bool = True
    checked_at: float = 0.0
    error: str | None = None
    failure_count: int = 0
    cooldown_until: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def aios_url(self) -> str:
        """aiohttp's per-request `proxy=` argument (http/https proxies)."""
        return self.raw

    def connector(self) -> "ProxyConnector | None":
        """Dedicated connector for SOCKS5 proxies (http(s) need none)."""
        if self.scheme != "socks5":
            return None
        if not HAVE_AIOHTTP_SOCKS or ProxyConnector is None:
            raise RuntimeError(
                "socks5:// proxies need the aiohttp-socks package (pip install aiohttp-socks)"
            )
        auth = None
        parts = urlsplit(self.raw)
        if parts.username:
            auth = (parts.username, parts.password or "")
        return ProxyConnector(host=self.host, port=self.port, rdns=True, username=parts.username, password=parts.password)

    def mark_failed(self, reason: str) -> None:
        self.failure_count += 1
        self.error = reason
        self.cooldown_until = time.monotonic() + COOLDOWN_S

    def available(self) -> bool:
        return self.alive and time.monotonic() >= self.cooldown_until


class ProxyManager:
    """All proxy decisions funnel through here — the downloader only asks."""

    def __init__(
        self,
        proxies: tuple[str, ...] | list[str],
        *,
        pin_domains: bool = False,
        check_url: str = "http://www.gstatic.com/generate_204",
        check_timeout: float = 8.0,
        ua_pool: tuple[str, ...] = UA_POOL,
    ) -> None:
        self.raw_list = [p.strip() for p in proxies if p and p.strip()]
        self.pin_domains = pin_domains
        self.check_url = check_url
        self.check_timeout = check_timeout
        self.ua_pool = tuple(ua_pool)
        # parse eagerly: a bad entry is a configuration error, fail before crawl
        self.proxies: list[Proxy] = [self._parse(p) for p in self.raw_list]
        self._rr = itertools.count()
        self._pinned: dict[str, Proxy] = {}
        self._ua_cycle = itertools.count()
        self._lock = asyncio.Lock()
        self._checked = False

    # ---- lifecycle -------------------------------------------------------

    async def health_check(self) -> None:
        """One lightweight request through each proxy; dead ones are skipped."""
        if self._checked or not self.proxies:
            return
        self._checked = True

        async def check_one(p: Proxy) -> None:
            connector = None
            try:
                session_kwargs: dict = {}
                if p.scheme == "socks5":
                    connector = p.connector()      # raises if unsupported
                    session_kwargs["connector"] = connector
                async with aiohttp.ClientSession(**session_kwargs) as s:
                    timeout = aiohttp.ClientTimeout(total=self.check_timeout)
                    proxy_arg = None if p.scheme == "socks5" else p.aios_url
                    async with s.get(self.check_url, timeout=timeout, proxy=proxy_arg) as resp:
                        if resp.status >= 400:
                            raise RuntimeError(f"health-check HTTP {resp.status}")
            except Exception as exc:               # noqa: BLE001 — any failure = dead proxy
                p.alive = False
                p.error = f"{type(exc).__name__}: {exc}"
            finally:
                p.checked_at = time.monotonic()
                if connector is not None:
                    await connector.close()

        await asyncio.gather(*(check_one(p) for p in self.proxies))

    @staticmethod
    def _parse(raw: str) -> Proxy:
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
        scheme = (parts.scheme or "http").lower()
        if scheme not in ("http", "https", "socks5"):
            raise ValueError(f"unsupported proxy scheme: {raw!r}")
        host = parts.hostname or ""
        port = parts.port or (1080 if scheme == "socks5" else 80)
        if not host:
            raise ValueError(f"proxy without a host: {raw!r}")
        return Proxy(raw=raw, scheme=scheme, host=host, port=port)

    # ---- rotation ----------------------------------------------------------

    @property
    def health_checked(self) -> bool:
        return self._checked

    @property
    def alive_count(self) -> int:
        return sum(1 for p in self.proxies if p.alive)

    def next_ua(self) -> str:
        if not self.ua_pool:
            return ""
        return self.ua_pool[next(self._ua_cycle) % len(self.ua_pool)]

    async def acquire(self, domain: str) -> Proxy | None:
        """Round-robin across healthy proxies; sticky when pinning is on."""
        async with self._lock:
            if not self.proxies:
                return None
            if self.pin_domains:
                pinned = self._pinned.get(domain)
                if pinned is not None and pinned.available():
                    return pinned
            healthy = [p for p in self.proxies if p.available()]
            if not healthy:
                # everything is cooling down: least-recently-failed wins so a
                # burst of failures degrades to sequential retries, not a stall
                healthy = [min(self.proxies, key=lambda p: p.cooldown_until)]
            pick = healthy[next(self._rr) % len(healthy)]
            if self.pin_domains:
                self._pinned[domain] = pick
            return pick

    def report_failure(self, proxy: Proxy, reason: str) -> None:
        """403/429/reset came back through *proxy* — cool it down so the next
        acquire() hands out a different one."""
        proxy.mark_failed(reason)
        # a pinned domain whose proxy just failed must re-pin elsewhere
        for d, p in list(self._pinned.items()):
            if p is proxy:
                del self._pinned[d]
