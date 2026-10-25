"""Preview in browser: runs a PreviewServer on a daemon thread and opens the
default browser at the mirror's local URL (phase 6)."""
from __future__ import annotations

import asyncio
import threading
import webbrowser

from PySide6.QtCore import QObject, Signal

from app.core.preview_server import PreviewServer


class PreviewController(QObject):
    started = Signal(str)      # bound url
    failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._server: PreviewServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._folder: str | None = None

    @property
    def url(self) -> str | None:
        if self._server is not None and self._folder is not None:
            return self._server.url
        return None

    def open_mirror(self, folder: str) -> None:
        """Serve *folder* (restarting only when the folder changed) and open
        the browser. Thread-safe: called from the GUI thread."""
        if self._loop is not None and self._folder == folder and self._server:
            webbrowser.open(self._server.url)          # already running
            self.started.emit(self._server.url)
            return
        self.stop()

        holder: dict[str, object] = {}

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._serve(folder, holder))
            except Exception as exc:                   # noqa: BLE001
                holder["error"] = f"{type(exc).__name__}: {exc}"

        threading.Thread(target=run, daemon=True).start()
        for _ in range(400):                           # up to ~2 s
            if "port" in holder or "error" in holder:
                break
            threading.Event().wait(0.005)
        if "port" in holder:
            url = f"http://127.0.0.1:{holder['port']}"
            webbrowser.open(url)
            self.started.emit(url)
        else:
            self.failed.emit(str(holder.get("error", "preview server failed to start")))

    async def _serve(self, folder: str, holder: dict) -> None:
        server = PreviewServer(folder)
        port = await server.start()
        self._server = server
        self._folder = folder
        holder["port"] = port
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await server.stop()

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        server, self._server = self._server, None
        self._folder = None
        if loop is not None and loop.is_running() and server is not None:
            loop.call_soon_threadsafe(loop.stop)
