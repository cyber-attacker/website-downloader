"""Phase 7 GUI: 'log in first' — open a visible browser, let the user
authenticate, capture the session cookies for the crawl."""
from __future__ import annotations

import asyncio

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


def cookies_to_header(cookies: list[dict]) -> str:
    """Playwright's context.cookies() -> a 'name=value; …' header string."""
    return "; ".join(f"{c['name']}={c['value']}"
                    for c in cookies if c.get("name"))


class LoginCaptureWorker(QThread):
    """Runs its own asyncio loop with a VISIBLE Chromium. The user logs in,
    the dialog's Done button calls finish(), cookies are emitted."""

    captured = Signal(str)          # cookie header string
    failed = Signal(str)
    started = Signal()

    def __init__(self, seed: str, parent=None) -> None:
        super().__init__(parent)
        self.seed = seed
        self._done = False

    def finish(self) -> None:
        """Called from the GUI thread when the user clicks Done."""
        self._done = True

    def run(self) -> None:
        asyncio.run(self._capture())

    async def _capture(self) -> None:
        try:
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            try:
                browser = await pw.chromium.launch(headless=False)
            except Exception as exc:               # noqa: BLE001
                raise RuntimeError(
                    "login capture needs a Playwright browser — run "
                    "'playwright install chromium' once") from exc
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(self.seed)
            except Exception:                      # noqa: BLE001 — user can navigate
                pass
            self.started.emit()
            while not self._done:
                await asyncio.sleep(0.25)
            try:
                cookies = await context.cookies()
                self.captured.emit(cookies_to_header(cookies))
            finally:
                await browser.close()
            await pw.stop()
        except Exception as exc:                   # noqa: BLE001 — surfaced to the GUI
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class LoginDialog(QDialog):
    """Holds the worker; Done -> finish(); captured -> accept()."""

    def __init__(self, seed: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Log in")
        self.cookie_header = ""
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "A browser window opened at the seed URL.\n"
            "Log in there, then press Done to capture the session cookies."))
        buttons = QHBoxLayout()
        done = QPushButton("Done — capture cookies", self)
        done.setDefault(True)
        done.clicked.connect(self._finish)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(done)
        layout.addLayout(buttons)

        self.worker = LoginCaptureWorker(seed, self)
        self.worker.captured.connect(self._on_captured)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _finish(self) -> None:
        self.setEnabled(False)
        self.worker.finish()

    def _on_captured(self, header: str) -> None:
        self.cookie_header = header
        self.accept()

    def _on_failed(self, msg: str) -> None:
        self.cookie_header = ""
        self.reject()
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Website Downloader", f"Login capture failed: {msg}")
