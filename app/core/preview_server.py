"""Local preview server: serve a completed mirror on http://localhost:<port>.

Why this exists (build guide, phase 6): file:// has no MIME negotiation —
extensionless or oddly-named mirrored files can fail to load as stylesheets
or scripts. A real HTTP server answers with proper Content-Types, serves
index.html for directory paths, and makes the finished mirror behave exactly
like the live site did.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import unquote

from aiohttp import web


class PreviewServer:
    """One mirror folder, one port. start() returns the bound port."""

    def __init__(self, root: str, port: int = 0, host: str = "127.0.0.1") -> None:
        self.root = root
        self.port = port
        self.host = host
        self._runner: web.AppRunner | None = None
        self.bound_port: int | None = None

    async def start(self) -> int:
        if self._runner is not None:
            return self.bound_port or self.port
        app = web.Application()
        root = os.path.abspath(self.root)

        async def serve(request):
            # resolve inside the mirror only (no ..-escapes), serve
            # index.html for directory URLs — the file:// pain point
            rel = unquote(request.url.path)
            candidate = os.path.abspath(os.path.join(root, rel.lstrip("/")))
            if candidate != root and not candidate.startswith(root + os.sep):
                raise web.HTTPForbidden(text="outside the mirror")
            if os.path.isdir(candidate):
                if not rel.endswith("/"):
                    raise web.HTTPFound(rel + "/")
                candidate = os.path.join(candidate, "index.html")
            if not os.path.isfile(candidate):
                raise web.HTTPNotFound(text="not in the mirror")
            return web.FileResponse(candidate)

        app.router.add_route("GET", "/{tail:.*}", serve)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        self.bound_port = site._server.sockets[0].getsockname()[1]
        return self.bound_port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self.bound_port = None

    @property
    def url(self) -> str:
        port = self.bound_port or self.port
        return f"http://{self.host}:{port}"
