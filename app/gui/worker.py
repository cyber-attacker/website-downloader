"""ProjectWorker: the only bridge between Qt and the async crawl engine.

Runs `Crawler.run()` inside its own QThread on a private asyncio loop and
forwards engine events as Qt signals (auto-queued to the GUI thread — the
worker never touches a widget). Pause/Resume/Cancel arrive from the GUI
thread via loop.call_soon_threadsafe, so the engine's asyncio primitives
are only ever mutated from inside the loop.
"""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QThread, Signal

from app.core.crawler import Crawler
from app.core.types import CrawlConfig


class ProjectWorker(QThread):
    # the phase-4 signal contract
    page_downloaded = Signal(str, str)      # url, output-relative path
    asset_downloaded = Signal(str, str)     # url, output-relative path
    progress_updated = Signal(dict)         # stats snapshot + paused/cancelled
    error_occurred = Signal(str)            # human-readable message
    state_changed = Signal(str)             # running|paused|resumed|cancelling|done|failed
    run_finished = Signal(object)           # CrawlReport

    def __init__(self, config: CrawlConfig, parent=None) -> None:
        super().__init__(parent)
        self.config = config
        self._crawler: Crawler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- QThread entry ----------------------------------------------------

    def run(self) -> None:                  # executes on its own thread
        asyncio.run(self._orchestrate())

    async def _orchestrate(self) -> None:
        self._loop = asyncio.get_running_loop()
        crawler = Crawler(self.config)
        self._crawler = crawler
        crawler.on_progress = self._emit
        self.state_changed.emit("running")
        try:
            report = await crawler.run()
        except Exception as exc:            # noqa: BLE001 — surfaced, never swallowed
            self.error_occurred.emit(f"crawl failed: {type(exc).__name__}: {exc}")
            self.state_changed.emit("failed")
            return
        self.run_finished.emit(report)
        self.state_changed.emit("done")

    # ---- engine -> Qt -------------------------------------------------------

    def _emit(self, kind: str, payload: dict) -> None:
        """Called on the worker's loop thread; Qt queues the emission."""
        if kind == "page":
            self.page_downloaded.emit(payload.get("url", ""), payload.get("path", ""))
        elif kind == "asset":
            self.asset_downloaded.emit(payload.get("url", ""), payload.get("path", ""))
        elif kind == "progress":
            self.progress_updated.emit(dict(payload))
        elif kind == "error":
            self.error_occurred.emit(
                f"{payload.get('url', '?')}: {payload.get('error', 'unknown error')}"
            )

    # ---- Qt -> engine (thread-safe) -------------------------------------------

    def _on_loop(self, fn) -> None:
        loop = self._loop
        crawler = self._crawler
        if loop is None or crawler is None or loop.is_closed():
            return
        if not loop.is_running():
            return
        loop.call_soon_threadsafe(fn)

    def pause(self) -> None:
        crawler = self._crawler
        if crawler is None or crawler.is_paused:
            return
        self._on_loop(crawler.pause)
        self.state_changed.emit("paused")

    def resume(self) -> None:
        crawler = self._crawler
        if crawler is None or not crawler.is_paused:
            return
        self._on_loop(crawler.resume)
        self.state_changed.emit("resumed")

    def cancel(self) -> None:
        crawler = self._crawler
        if crawler is None:
            return
        self._on_loop(crawler.request_cancel)
        self.state_changed.emit("cancelling")
