"""Shared types for the website-downloader core engine.

These dataclasses are intentionally GUI-agnostic: both the CLI entry point and
the future PySide6 layer must be able to construct them.
"""
from __future__ import annotations

import enum
import time
from collections import deque
from dataclasses import dataclass, field

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 WebsiteDownloader/0.1"
)


class ResourceKind(str, enum.Enum):
    PAGE = "page"
    ASSET = "asset"


@dataclass(frozen=True)
class CrawlConfig:
    """Immutable settings for one crawl run."""

    seed: str
    output_dir: str
    depth: int = 3
    concurrency: int = 8
    per_host_concurrency: int = 4
    delay: float = 0.0                 # min seconds between requests to same host
    timeout: float = 30.0
    retries: int = 3
    backoff_base: float = 0.7          # seconds; doubles per attempt + jitter
    max_pages: int = 0                 # 0 = unlimited
    max_file_size: int = 50 * 1024 * 1024
    user_agent: str = DEFAULT_UA
    respect_robots: bool = True
    allow_subdomains: bool = False
    exclude: tuple[str, ...] = ()
    verify_links: bool = True
    state_db: str | None = None      # <output>/project.db enables persistence + resume
    proxies: tuple[str, ...] = ()    # phase 3: rotating proxy pool
    pin_domain_proxy: bool = False   # same domain always uses the same proxy
    rotate_user_agent: bool = False  # rotate UA per request alongside proxies
    proxy_check_url: str = "http://www.gstatic.com/generate_204"
    build_search_index: bool = True  # phase 6: <output>/search.db (FTS5)
    headless: bool = False           # phase 7: render pages with headless Chromium
    cookies: tuple[str, ...] = ()    # phase 7: "name=value" pieces sent everywhere

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("depth must be >= 0")
        if not (1 <= self.concurrency <= 512):
            raise ValueError("concurrency must be between 1 and 512")
        if self.delay < 0 or self.timeout <= 0 or self.retries < 0:
            raise ValueError("delay/timeout/retries out of range")


@dataclass(frozen=True)
class Job:
    """One unit of work in the crawl queue."""

    url: str              # normalized absolute URL
    depth: int            # link-hops from the seed (for pages)
    referer: str | None = None


@dataclass
class Failure:
    url: str
    stage: str            # "download" | "robots" | "rewrite" | "save"
    error: str
    status: int | None = None
    attempts: int = 0
    ts: float = field(default_factory=time.time)


class FetchStage(enum.Enum):
    OK = "ok"
    FAILED = "failed"


@dataclass
class FetchResult:
    ok: bool
    url: str                        # requested (normalized) URL
    final_url: str = ""             # after redirects
    redirect_from: str | None = None  # normalized pre-redirect URL, if any
    status: int | None = None
    content_type: str = ""
    charset: str | None = None
    etag: str | None = None            # for conditional re-requests on a later run
    last_modified: str | None = None
    data: bytes = b""
    attempts: int = 0
    error: str | None = None
    too_large: bool = False


@dataclass
class ResourceRecord:
    """One saved or attempted resource, dumped into manifest.json."""

    url: str
    kind: str                       # "page" | "asset"
    path: str | None = None         # output-relative POSIX path, when saved
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    charset: str | None = None
    size: int = 0
    rewritten: bool = False         # True after the finalize rewrite pass


@dataclass
class CrawlStats:
    pages_seen: int = 0
    assets_seen: int = 0
    pages_saved: int = 0
    assets_saved: int = 0
    pages_failed: int = 0
    assets_failed: int = 0
    bytes_downloaded: int = 0
    redirects: int = 0
    skipped_robots: int = 0
    skipped_excluded: int = 0
    skipped_max_pages: int = 0
    skipped_depth: int = 0
    skipped_oversized: int = 0
    started: float = field(default_factory=time.monotonic)
    _window: deque[tuple[float, int]] = field(default_factory=deque, repr=False)

    def add_bytes(self, n: int) -> None:
        self.bytes_downloaded += n
        now = time.monotonic()
        self._window.append((now, n))
        while self._window and now - self._window[0][0] > 10.0:
            self._window.popleft()

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 1e-6)

    @property
    def recent_speed(self) -> float:
        """Bytes per second over the last 10 seconds."""
        if not self._window:
            return 0.0
        span = max(time.monotonic() - self._window[0][0], 0.25)
        return sum(n for _, n in self._window) / span

    def snapshot(self) -> dict:
        return {
            "pages_seen": self.pages_seen,
            "assets_seen": self.assets_seen,
            "pages_saved": self.pages_saved,
            "assets_saved": self.assets_saved,
            "pages_failed": self.pages_failed,
            "assets_failed": self.assets_failed,
            "bytes": self.bytes_downloaded,
            "redirects": self.redirects,
            "mbps_recent": round(self.recent_speed / 1_000_000, 2),
            "elapsed_s": round(self.elapsed, 1),
        }


@dataclass
class CrawlReport:
    config: CrawlConfig
    stats: CrawlStats
    records: list[ResourceRecord]
    failures: list[Failure]
    redirects: dict[str, str]
    out_of_scope: set[str]           # URLs intentionally not downloaded (depth/robots/etc.)
    duration: float
    output_dir: str
    resumed_pending: int = 0          # phase 2: unfinished rows requeued on resume
    resumed_done: int = 0            # phase 2: finished rows kept on disk untouched

    def summary_text(self) -> str:
        s = self.stats
        lines = [
            f"Elapsed: {self.duration:.1f}s",
            f"Pages: {s.pages_saved} saved, {s.pages_failed} failed ({s.pages_seen} discovered)",
            f"Assets: {s.assets_saved} saved, {s.assets_failed} failed ({s.assets_seen} discovered)",
            f"Downloaded: {s.bytes_downloaded / 1_000_000:.2f} MB ({s.redirects} redirects)",
            f"Skipped: robots={s.skipped_robots} excluded={s.skipped_excluded} "
            f"depth={s.skipped_depth} max_pages={s.skipped_max_pages}",
        ]
        if self.out_of_scope:
            lines.append(f"Out-of-scope URLs left absolute: {len(self.out_of_scope)}")
        if self.failures:
            lines.append(f"Failures: {len(self.failures)}")
            for f in self.failures[:10]:
                lines.append(f"  [{f.stage}] {f.url} -> {f.error}")
            if len(self.failures) > 10:
                lines.append(f"  ... and {len(self.failures) - 10} more (see manifest.json)")
        if s.skipped_oversized:
            lines.append(f"Oversized files skipped: {s.skipped_oversized}")
        return "\n".join(lines)
