"""Disk storage: URL->path mapping, atomic writes, manifest bookkeeping."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict

from app.core.types import ResourceRecord
from app.utils.url_utils import map_url_to_rel, rel_for_attr


class Storage:
    """Owns the output directory: mapping, writes and the run manifest."""

    def __init__(
        self,
        root: str,
        seed_host: str,
        allow_subdomains: bool = False,
    ) -> None:
        self.root = os.path.abspath(root)
        self.seed_host = seed_host
        self.allow_subdomains = allow_subdomains
        os.makedirs(self.root, exist_ok=True)
        self.records: list[ResourceRecord] = []
        self.redirect_map: dict[str, str] = {}

    # ---- mapping ---------------------------------------------------------

    def rel_path(self, url: str, kind: str | None = None) -> str:
        """Output-relative POSIX path for a normalized URL (pure)."""
        return map_url_to_rel(url, self.seed_host, self.allow_subdomains, kind_override=kind)

    # ---- seed landing page -------------------------------------------------

    def needs_seed_redirect(self, seed_rel: str) -> bool:
        return seed_rel != "index.html" and seed_rel.rstrip("/") != ""

    def write_seed_redirect(self, seed_rel: str) -> None:
        """Auto-generate <root>/index.html redirect if seed wasn't the homepage."""
        target = rel_for_attr("index.html", seed_rel)
        html = (
            "<!DOCTYPE html>\n"
            '<html><head><meta charset="utf-8">\n'
            f'<meta http-equiv="refresh" content="0; url={target}">\n'
            f'<title>Mirror entry point</title></head>\n'
            f'<body><p>The mirror starts at <a href="{target}">{target}</a> '
            "(this offline copy was taken from a non-homepage URL).</p>\n"
            "</body></html>\n"
        )
        self._write_sync("index.html", html.encode("utf-8"))

    def resolve_redirect(self, url: str) -> str:
        seen: set[str] = set()
        while url in self.redirect_map and url not in seen:
            seen.add(url)
            url = self.redirect_map[url]
        return url

    def abs_path(self, rel: str) -> str:
        return os.path.join(self.root, *rel.split("/"))

    # ---- writing ---------------------------------------------------------

    async def write(self, rel: str, data: bytes) -> bool:
        """Atomic write. Returns True if a new file was created."""
        created = await asyncio.to_thread(self._write_sync, rel, data)
        return created

    def _write_sync(self, rel: str, data: bytes) -> bool:
        abs_path = self.abs_path(rel)
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{abs_path}.tmp-{int(time.time() * 1000) % 10**9}{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp, abs_path)
        except OSError:
            os.replace(tmp, abs_path)
        return True

    # ---- bookkeeping -----------------------------------------------------

    def add_record(self, rec: ResourceRecord) -> None:
        self.records.append(rec)

    def add_redirect(self, src: str, dst: str) -> None:
        """Redirect map used while rewriting + dumped into the manifest."""
        self.redirect_map.setdefault(src, dst)

    def dump_manifest(self, stats, failures) -> None:
        payload = {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seed_host": self.seed_host,
            "output_root": self.root,
            "stats": stats.snapshot(),
            "redirects": dict(sorted(self.redirect_map.items())),
            "resources": [asdict(r) for r in self.records],
            "failures": [
                {
                    "url": f.url,
                    "stage": f.stage,
                    "error": f.error,
                    "status": f.status,
                    "attempts": f.attempts,
                }
                for f in failures
            ],
        }
        path = os.path.join(self.root, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
