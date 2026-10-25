"""Preview in browser: runs a PreviewServer on a daemon thread and opens the
default browser at the mirror's local URL (phase 6). The loop owns its own
shutdown — stop() just sets an asyncio.Event from outside."""
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._url: str | None = None
        self._folder: str | None = None

    @property
    def url(self) -> str | None:
        return self._url

    def open_mirror(self, folder: str) -> None:
        """Serve *folder* (restarting only when the folder changed) and open
        the browser. Thread-safe: called from the GUI thread."""
        if self._folder == folder and self._url:
            webbrowser.open(self._url)               # already running
            self.started.emit(self._url)
            return
        self.stop()
        holder: dict[str, object] = {}

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stop_event = asyncio.Event()
            try:
                loop.run_until_complete(self._serve(folder, holder))
            except Exception as exc:                 # noqa: BLE001
                holder["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                loop.close()

        threading.Thread(target=run, daemon=True).start()
        for _ in range(400):                         # up to ~2 s
            if "port" in holder or "error" in holder:
                break
            threading.Event().wait(0.005)
        if "port" in holder:
            self._url = f"http://127.0.0.1:{holder['port']}"
            webbrowser.open(self._url)
            self.started.emit(self._url)
        else:
            self.failed.emit(str(holder.get("error", "preview server failed to start")))

    async def _serve(self, folder: str, holder: dict) -> None:
        server = PreviewServer(folder)
        port = await server.start()
        self._folder = folder
        holder["port"] = port
        try:
            await self._stop_event.wait()
        finally:
            await server.stop()
            self._url = None
            self._folder = None

    def stop(self) -> None:
        loop, self._loop = self._loop, None
        event = self._stop_event
        self._stop_event = None
        self._url = None
        self._folder = None
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
