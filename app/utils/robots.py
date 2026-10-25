"""Politeness: robots.txt loading and permission checks.

Unreachable or unparseable robots.txt is treated as "allowed" (a mirror tool
that blocks itself whenever robots.txt fails to load would be useless), while
explicit Disallow rules are always honored unless the user opts out.
"""
from __future__ import annotations

import asyncio
import urllib.robotparser as robotparser
from urllib.parse import urlsplit


class RobotsCache:
    def __init__(self, session_factory, timeout: float = 10.0) -> None:
        self._session_factory = session_factory
        self._timeout = timeout
        self._cache: dict[str, robotparser.RobotFileParser | None] = {}
        self._delays: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def _fetch_for_host(self, base_url: str, ua: str):
        parts = urlsplit(base_url)
        key_netloc = parts.netloc or parts.hostname
        rp = robotparser.RobotFileParser()
        tried = [parts.scheme or "http"] + [s for s in ("http", "https") if s != parts.scheme]
        for scheme in tried:
            url = f"{scheme}://{key_netloc}/robots.txt"
            try:
                async with self._session_factory().get(url, timeout=self._timeout) as resp:
                    if resp.status >= 400:
                        rp = None   # allow all
                        break
                    rp.parse((await resp.text(errors="replace")).splitlines())
                    break
            except Exception:
                continue
        else:
            rp = None               # network failure -> allow
        return rp

    async def allowed(self, url: str, ua: str) -> tuple[bool, float]:
        """Return (allowed?, crawl_delay_seconds_for_host)."""
        parts = urlsplit(url)
        host = (parts.netloc or parts.hostname or "").lower()
        async with self._lock_for(host):
            if host not in self._cache:
                self._cache[host] = await self._fetch_for_host(url, ua)
        rp = self._cache[host]
        if rp is None:
            return True, 0.0
        try:
            ok = rp.can_fetch(ua, url)
            if not ok:
                ok = rp.can_fetch("*", url)
        except Exception:
            ok = True
        delay = 0.0
        try:
            d = rp.crawl_delay(ua) or rp.crawl_delay("*")
            if d:
                delay = float(d)
        except Exception:
            pass
        return ok, delay
