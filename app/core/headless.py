"""Optional Playwright headless-browser mode (phase 7).

Renders page URLs with headless Chromium so client-side-injected content and
links are captured. Plugs into the crawler at fetch time: downstream storage,
rewriting, resume and search work unchanged on the rendered HTML.

Browsers are NOT bundled with the app — `playwright install chromium` is a
one-time, user-initiated step (the GUI shows the prompt when needed).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.types import CrawlConfig, FetchResult, Job


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    """'session=abc; token=xyz' -> [('session','abc'), ('token','xyz')]."""
    out: list[tuple[str, str]] = []
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        out.append((name.strip(), value.strip()))
    return out


def cookie_header(cookies: tuple[str, ...] | list[str]) -> str:
    """Config cookie entries (already 'name=value' pieces) -> one header."""
    return "; ".join(c.strip() for c in cookies if c.strip())


@dataclass
class RenderResult:
    html: str
    final_url: str
    status: int | None = None
    error: str | None = None


class HeadlessRenderer:
    """One browser context for the whole crawl; renders page jobs."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._pw = None
        self._browser = None
        self._context = None

    async def ensure_started(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True)
        except Exception as exc:                 # noqa: BLE001 — missing binaries etc.
            raise RuntimeError(
                "headless mode needs a Playwright browser — run "
                "'playwright install chromium' once"
            ) from exc
        kwargs: dict = {"user_agent": self.config.user_agent}
        self._context = await self._browser.new_context(**kwargs)
        if self.config.cookies:
            await self._context.add_cookies([
                {"name": n, "value": v, "url": self.config.seed}
                for n, v in self._iter_cookies()
            ])

    def _iter_cookies(self):
        for c in self.config.cookies:
            for n, v in parse_cookie_header(c):
                if n:
                    yield n, v

    async def render(self, url: str, timeout: float | None = None) -> RenderResult:
        await self.ensure_started()
        assert self._context is not None
        page = await self._context.new_page()
        try:
            resp = await page.goto(url, wait_until="load",
                                    timeout=(timeout or self.config.timeout) * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:                    # noqa: BLE001 — some pages never go idle
                pass
            html = await page.content()
            return RenderResult(html=html, final_url=page.url,
                                status=resp.status if resp else None)
        except Exception as exc:                # noqa: BLE001 — surfaced as a fetch error
            return RenderResult(html="", final_url=url, error=f"{type(exc).__name__}: {exc}")
        finally:
            await page.close()

    async def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"),
                          (self._pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, meth)()
                except Exception:                # noqa: BLE001 — teardown must not raise
                    pass
        self._context = self._browser = self._pw = None


async def render_fetch(renderer: HeadlessRenderer, job: Job,
                       config: CrawlConfig) -> FetchResult:
    """Playwright render -> FetchResult, so the crawler's process path is
    identical for http and headless modes."""
    r = await renderer.render(job.url)
    if r.error is not None:
        return FetchResult(ok=False, url=job.url, final_url=r.final_url,
                           status=r.status, error=r.error, attempts=1)
    data = r.html.encode("utf-8", errors="xmlcharrefreplace")
    return FetchResult(
        ok=(r.status or 200) < 400,
        url=job.url, final_url=r.final_url,
        status=r.status or 200,
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        data=data,
        attempts=1,
        error=None if (r.status or 200) < 400 else f"HTTP {r.status}",
    )
