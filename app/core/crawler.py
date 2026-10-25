"""Crawler orchestrator: BFS queue, scope control, parsing and the two-pass
offline rewrite.

Design notes
------------
- One asyncio queue feeds ``concurrency`` workers; every job is processed then
  marked done, so ``queue.join()`` fires only when the whole frontier is empty
  (crash-safe resume comes in phase 2 on top of this structure).
- HTML/CSS files are written to disk *raw* during the run and rewritten in a
  finalize pass with the complete redirect map — this is what makes links to
  redirected URLs resolve to the right local file.
- Out-of-scope URLs (beyond depth, robots-blocked, excluded, over max_pages)
  and foreign *pages* are left as absolute URLs in the mirror.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.downloader import Downloader
from app.core.rewriter import (
    absolutize,
    extract_css,
    extract_html,
    extract_style_urls,
    rewrite_css,
    rewrite_html,
)
from app.core.storage import Storage
from app.core.state_db import StateDB, content_hash_of
from app.core.types import (
    CrawlConfig,
    CrawlReport,
    CrawlStats,
    Failure,
    FetchResult,
    Job,
    ResourceRecord,
)
from app.utils.robots import RobotsCache
from app.utils.url_utils import doc_base, in_scope, is_page_url, normalize


def _is_html(ctype: str, url: str) -> bool:
    c = (ctype or "").lower()
    if "text/html" in c or "application/xhtml" in c:
        return True
    return not c and is_page_url(url)


def _is_css(ctype: str, url: str) -> bool:
    c = (ctype or "").lower()
    if "text/css" in c:
        return True
    return not c and url.lower().split("?")[0].endswith(".css")


class Crawler:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.seed_norm = normalize(config.seed)
        if not self.seed_norm:
            raise ValueError(f"invalid seed URL: {config.seed}")
        self.seed_scheme = urlsplit(self.seed_norm).scheme
        # netloc (host:port, without credentials) — keeps same-host-different-port
        # origins distinct and still names the foreign-CDN foldering correctly
        self.seed_host = urlsplit(self.seed_norm).netloc.split("@")[-1] or ""
        self.storage = Storage(config.output_dir, self.seed_host, config.allow_subdomains)
        self.downloader = Downloader(config)
        self.robots = RobotsCache(self.downloader.session_factory)
        self.downloader.robots = self.robots
        self.stats = CrawlStats()
        self.failures: list[Failure] = []
        self.out_of_scope: set[str] = set()
        self.redirects: dict[str, str] = {}
        self._seen: set[str] = set()
        self._pages_enqueued = 0
        self._seed_job: Job | None = None
        # persistence (None = pure in-memory run, exactly the phase-1 behavior)
        self.state = StateDB(config.state_db) if config.state_db else None
        self.resumed_pending = 0
        self.resumed_done = 0
        self._prev_finalized = False
        # GUI control surface (phase 4): set = running, cleared = paused
        self._pause = asyncio.Event()
        self._pause.set()
        self._cancel_requested = False
        self.on_progress = None           # optional callable(kind: str, payload: dict)
        self._renderer = None             # phase 7: lazy HeadlessRenderer

    # ------------------------------------------------------------------ run

    async def run(self) -> CrawlReport:
        self.downloader.on_redirect = self._track_redirect
        await self.downloader.setup_proxies()
        queue: asyncio.Queue[Job] = asyncio.Queue()
        started = time.monotonic()
        if self.state:
            await self.state.open()
            meta = await self.state.load_meta()
            await self.state.save_meta({
                "schema_version": 1,     # future releases migrate on this
                "seed_url": self.config.seed,
                "seed_normalized": self.seed_norm,
                "max_depth": self.config.depth,
                "concurrency": self.config.concurrency,
                "output_dir": self.config.output_dir,
                "exclude": list(self.config.exclude),
                "respect_robots": self.config.respect_robots,
                "allow_subdomains": self.config.allow_subdomains,
                "created_at": meta.get("created_at", time.time()),
                "last_run_at": time.time(),
                "finalized": False,     # this run's rewrite pass hasn't happened yet
            })
            self._prev_finalized = bool(meta.get("finalized", False))
            await self._resume(queue)
        if self.seed_norm not in self._seen:
            seed = Job(self.seed_norm, 0)
            self._seen.add(self.seed_norm)
            self._pages_enqueued += 1
            self.stats.pages_seen += 1
            self._seed_job = seed
            if self.state:
                await self.state.ensure_url(self.seed_norm, 0)
            queue.put_nowait(seed)

        workers = [asyncio.create_task(self._worker(queue)) for _ in range(self.config.concurrency)]
        self._queue = queue
        ticker = asyncio.create_task(self._progress_ticker())
        try:
            try:
                await queue.join()
            finally:
                for t in workers:
                    t.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                ticker.cancel()
            await self.downloader.close()

            await self._finalize_rewrites()
            if self.state:
                # files on disk are now final; a future resume may trust them
                await self.state.save_meta({"finalized": True})
            self._write_seed_redirect()
            self.storage.dump_manifest(self.stats, self.failures)
        finally:
            if self.state:
                await self.state.close()
            if self._renderer is not None:
                await self._renderer.close()
                self._renderer = None
        duration = time.monotonic() - started
        return CrawlReport(
            config=self.config,
            stats=self.stats,
            records=self.storage.records,
            failures=self.failures,
            redirects=dict(self.storage.redirect_map),
            out_of_scope=set(self.out_of_scope),
            duration=duration,
            output_dir=self.config.output_dir,
            resumed_pending=self.resumed_pending,
            resumed_done=self.resumed_done,
        )

    async def _resume(self, queue: asyncio.Queue[Job]) -> None:
        """Re-open a project: re-queue unfinished work, keep finished files,
        rebuild enough in-memory state (seen set, records) for finalize/verify."""
        known = await self.state.all_known_urls()
        self._seen = set(known)
        # previous-run skips/failures behave exactly like fresh out-of-scope
        # discoveries: their references stay absolute, nothing re-fetches
        self.out_of_scope |= await self.state.skipped_or_failed_urls()
        for row in await self.state.hydrate_records():
            rec = ResourceRecord(
                url=row["url"],
                kind="page" if is_page_url(row["url"]) else "asset",
                path=row["local_path"],
                final_url=row["url"],
                status=200,
                content_type=row["content_type"] or "",
                charset=None,
                size=0,
                # only trustworthy if the previous run completed its rewrite
                # pass; a killed run leaves raw files that must be finalized
                rewritten=getattr(self, "_prev_finalized", False),
            )
            self.storage.add_record(rec)
        self.resumed_done = len(self.storage.records)
        pending = await self.state.get_pending()
        for p in pending:
            if is_page_url(p.url):
                self._pages_enqueued += 1
                self.stats.pages_seen += 1
            else:
                self.stats.assets_seen += 1
            queue.put_nowait(Job(p.url, p.depth, None))
        self.resumed_pending = len(pending)

    # -------------------------------------------------- GUI control (phase 4)

    @property
    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def pause(self) -> None:
        """Stop taking new queue items; in-flight requests finish first.
        Call from the loop thread (the GUI worker wraps this in
        call_soon_threadsafe)."""
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def request_cancel(self) -> None:
        """Finish quickly: workers drain the queue without working; the
        finalize/manifest passes still run so the partial mirror is valid."""
        self._cancel_requested = True
        self._pause.set()      # wake paused workers so they can drain

    async def _progress_ticker(self) -> None:
        while True:
            if self.on_progress is not None:
                try:
                    self.on_progress("progress", {
                        **self.stats.snapshot(),
                        "paused": self.is_paused,
                        "cancelled": self._cancel_requested,
                        "out_of_scope": len(self.out_of_scope),
                        "queue_backlog": self._queue.qsize() if getattr(self, "_queue", None) else 0,
                    })
                except Exception:            # noqa: BLE001 — a GUI callback must never kill the crawl
                    pass
            await asyncio.sleep(0.4)

    async def _worker(self, queue: asyncio.Queue[Job]) -> None:
        while True:
            job = await queue.get()
            try:
                if self._cancel_requested:
                    continue                # drain without working
                if not self._pause.is_set():
                    await self._pause.wait()
                    if self._cancel_requested:
                        continue
                await self._process(job, queue)
            except Exception as exc:               # noqa: BLE001 — a worker must never die mid-queue
                self.failures.append(
                    Failure(url=job.url, stage="internal", error=f"{type(exc).__name__}: {exc}")
                )
                if self.on_progress is not None:
                    self.on_progress("error", {
                        "url": job.url, "error": f"{type(exc).__name__}: {exc}",
                    })
            finally:
                queue.task_done()

    async def _fetch(self, job: Job) -> FetchResult:
        """HTTP by default; headless Chromium for page jobs when enabled."""
        cfg = self.config
        if not (cfg.headless and is_page_url(job.url)):
            return await self.downloader.fetch(job)   # robots + politeness inside

        # headless path: apply the same gates the HTTP path applies internally
        if cfg.respect_robots and self.robots:
            try:
                allowed, robots_delay = await self.robots.allowed(job.url, cfg.user_agent)
            except Exception:                        # noqa: BLE001
                allowed, robots_delay = True, 0.0
            if not allowed:
                return FetchResult(ok=False, url=job.url,
                                   error="robots.txt disallowed", attempts=0)
            if cfg.delay or robots_delay:
                await self.downloader.wait_turn(job.url, robots_delay)

        from app.core.headless import HeadlessRenderer, render_fetch

        if self._renderer is None:
            self._renderer = HeadlessRenderer(cfg)
        res = await render_fetch(self._renderer, job, cfg)
        if res.ok and res.final_url and res.final_url != job.url:
            self._track_redirect(job.url, res.final_url)
        return res

    async def _process(self, job: Job, queue: asyncio.Queue[Job]) -> None:
        url = job.url
        if self.state:
            await self.state.mark_status(url, "downloading")
        res = await self._fetch(job)
        if not res.ok:
            await self._record_failure(res)
            return
        stats = self.stats
        stats.add_bytes(len(res.data))

        final_norm = normalize(res.final_url, site_host=self.seed_host, site_scheme=self.seed_scheme) or url
        self._seen.add(final_norm)

        # route by CONTENT first (a .php url may serve images, /css?query serves css)
        charset = sniff_charset_of(res)
        text_like = _is_html(res.content_type, final_norm)
        css_like = _is_css(res.content_type, final_norm)
        kind = "page" if text_like else "asset"

        # a redirect may land on an excluded URL — honor the exclusion
        if self._excluded(final_norm):
            self.stats.skipped_excluded += 1
            self.out_of_scope.add(final_norm)
            self.storage.add_record(ResourceRecord(
                url=url, kind=kind, path=None, final_url=final_norm,
                status=res.status, content_type=res.content_type,
                charset=None, size=0,
            ))
            self.failures.append(Failure(url=final_norm, stage="excluded",
                                         error="redirect target excluded by rules",
                                         status=res.status, attempts=1))
            if self.state:
                await self.state.record_skip(final_norm, job.depth,
                                             "redirect target excluded by rules")
            return

        self.storage.add_record(ResourceRecord(
            url=url, kind=kind, path=None, final_url=final_norm,
            status=res.status, content_type=res.content_type, charset=charset,
            size=len(res.data),
        ))
        rec = self.storage.records[-1]

        if text_like or css_like:
            try:
                text = res.data.decode(charset, errors="replace")
            except (UnicodeError, LookupError):
                charset = "utf-8"
                rec.charset = charset
                text = res.data.decode("utf-8", errors="replace")
        else:
            text = None

        if text_like:
            await self._process_html(job, queue, final_norm, text or "", rec)  # type: ignore[arg-type]
        elif css_like:
            await self._process_css(queue, final_norm, text or "", rec)  # type: ignore[arg-type]
        else:
            # plain asset: write and forget
            rel = self.storage.rel_path(final_norm, "asset")
            rec.path = rel
            await self.storage.write(rel, res.data)
            stats.assets_saved += 1
            if self.on_progress is not None:
                self.on_progress("asset", {"url": url, "path": rel})

        # journal LAST, after the file is on disk and every child URL is
        # already inserted as pending: a crash before this line only costs a
        # re-fetch of this one URL, never a lost branch of the tree
        if self.state:
            await self.state.mark_status(
                url, "done",
                local_path=rec.path,
                content_hash=content_hash_of(res.data),
                content_type=(rec.content_type or res.content_type or None),
                etag=res.etag,
                last_modified=res.last_modified,
            )

    # ----------------------------------------------------------- HTML pages

    async def _process_html(
        self, job: Job, queue: asyncio.Queue[Job], final_norm: str, text: str, rec: ResourceRecord
    ) -> None:
        cfg = self.config
        save_raw = True
        ext = extract_html(text)
        base_abs = doc_base(final_norm)
        if ext.base_href:
            base_abs = absolutize(ext.base_href, base_abs) or base_abs

        # write the raw file now; the finalize pass rewrites it with the full
        # redirect map
        rec.path = self.storage.rel_path(final_norm, "page")
        await self.storage.write(rec.path, _raw_bytes(text, rec.charset or "utf-8"))
        self.stats.pages_saved += 1
        if self.on_progress is not None:
            self.on_progress("page", {"url": job.url, "path": rec.path,
                                      "referer": job.referer})

        # ---- enqueue children
        for ref in ext.links:
            norm = normalize(ref.url, base_abs, site_host=self.seed_host, site_scheme=self.seed_scheme)
            if not norm:
                continue
            await self._enqueue_link(norm, job.depth + 1, queue, final_norm)
        for ref in ext.assets:
            norm = normalize(ref.url, base_abs, site_host=self.seed_host, site_scheme=self.seed_scheme)
            if not norm:
                continue
            await self._enqueue_asset(norm, queue, referer=final_norm)
        for u in extract_style_urls(text):
            norm = normalize(u, final_norm, site_host=self.seed_host, site_scheme=self.seed_scheme)
            if norm:
                await self._enqueue_asset(norm, queue, referer=final_norm)

    async def _enqueue_link(self, norm: str, depth: int, queue: asyncio.Queue[Job], referer: str) -> None:
        cfg = self.config
        if self._excluded(norm):
            self.stats.skipped_excluded += 1
            self.out_of_scope.add(norm)
            if self.state:
                await self.state.record_skip(norm, depth, "excluded by rules")
            return
        if not in_scope((urlsplit(norm).netloc or urlsplit(norm).hostname or ""), self.seed_host,
                        cfg.allow_subdomains):
            return  # foreign page: left absolute, not crawled, not journaled
        if not is_page_url(norm):                     # linked PDF/zip/etc — asset
            await self._enqueue_asset(norm, queue, referer=referer)
            return
        if norm in self._seen:
            return
        self._seen.add(norm)
        if depth > cfg.depth:
            self.stats.skipped_depth += 1
            self.out_of_scope.add(norm)
            if self.state:
                await self.state.record_skip(norm, depth, f"beyond max depth {cfg.depth}")
            return
        if cfg.max_pages and self._pages_enqueued >= cfg.max_pages:
            self.stats.skipped_max_pages += 1
            self.out_of_scope.add(norm)
            if self.state:
                await self.state.record_skip(norm, depth, "max-pages limit reached")
            return
        self._pages_enqueued += 1
        self.stats.pages_seen += 1
        if self.state:
            await self.state.ensure_url(norm, depth, referer)
        queue.put_nowait(Job(norm, depth, referer))

    async def _enqueue_asset(self, norm: str, queue: asyncio.Queue[Job], referer: str | None = None) -> None:
        if norm in self._seen:
            return
        self._seen.add(norm)
        if self._excluded(norm):
            self.stats.skipped_excluded += 1
            self.out_of_scope.add(norm)
            if self.state:
                await self.state.record_skip(norm, 0, "excluded by rules")
            return
        self.stats.assets_seen += 1
        if self.state:
            await self.state.ensure_url(norm, 0, referer)
        queue.put_nowait(Job(norm, 0, referer))

    def _excluded(self, url: str) -> bool:
        return any(fnmatch.fnmatch(url, pat) for pat in self.config.exclude)

    # --------------------------------------------------------------- CSS

    async def _process_css(
        self, queue: asyncio.Queue[Job], final_norm: str, text: str, rec: ResourceRecord
    ) -> None:
        rec.path = self.storage.rel_path(final_norm, "asset")
        if not rec.path.endswith(".css"):
            # e.g. /css?family=Raleway — extension folded into a hashed stem;
            # give file:// browsers the cue to treat the file as a stylesheet
            rec.path += ".css"
        rec.charset = rec.charset or "utf-8"
        await self.storage.write(rec.path, _raw_bytes(text, rec.charset))
        self.stats.assets_saved += 1
        if self.on_progress is not None:
            self.on_progress("asset", {"url": final_norm, "path": rec.path})
        for u in extract_css(text):
            norm = normalize(u, final_norm, site_host=self.seed_host, site_scheme=self.seed_scheme)
            if norm:
                await self._enqueue_asset(norm, queue, referer=final_norm)

    # ---------------------------------------------------- redirect helpers

    def _track_redirect(self, src: str, dst: str) -> None:
        s = normalize(src, site_host=self.seed_host, site_scheme=self.seed_scheme) or src
        d = normalize(dst, site_host=self.seed_host, site_scheme=self.seed_scheme) or dst
        if s and d and s != d:
            self.storage.add_redirect(s, d)
            self.redirects[s] = d
            self.stats.redirects += 1

    # --------------------------------------------------- finalize rewriting

    def normalize_for_site(self, url: str) -> str | None:
        return normalize(url, site_host=self.seed_host, site_scheme=self.seed_scheme)

    def _build_index(self) -> None:
        """Key every record by its request URL and final URL; saved paths from
        the crawl are authoritative for finalize + verify."""
        self._recs_by_url: dict[str, ResourceRecord] = {}
        for rec in self.storage.records:
            for key in (rec.url, rec.final_url):
                if not key:
                    continue
                prev = self._recs_by_url.get(key)
                if prev is None or (not prev.path and rec.path):
                    self._recs_by_url[key] = rec

    def _target_rec(self, final_url: str) -> ResourceRecord | None:
        return self._recs_by_url.get(final_url)

    def _resolve_saved(self, final_url: str) -> str | None:
        """Output path for a normalized URL — the value recorded at crawl time
        wins, so content-driven saving (/css?query serving text/css) and the
        rewrite/verify passes can never disagree."""
        rec = self._recs_by_url.get(final_url)
        if rec is not None and rec.path:
            return rec.path
        if rec is not None:
            return None          # attempted but not saved (failed/excluded)
        return self.storage.rel_path(final_url)

    def _resolver(self):
        storage = self.storage

        def resolver(abs_url: str, kind: str) -> str | None:
            norm = self.normalize_for_site(abs_url)
            if not norm:
                return None
            final = storage.resolve_redirect(norm)
            # depth/robots/excluded/beyond-scope: leave absolute
            if final in self.out_of_scope:
                return None
            target = self._target_rec(final)
            if target is not None and not target.path:
                return None      # download failed: leave the URL absolute (goes online)
            if not in_scope((urlsplit(final).netloc or urlsplit(final).hostname or ""), self.seed_host,
                            self.config.allow_subdomains) and kind == "page":
                return None      # foreign host: pages stay absolute, assets still mirror
            return self._resolve_saved(final)

        return resolver

    async def _finalize_rewrites(self) -> None:
        self._build_index()
        resolver = self._resolver()
        rewritten_paths: set[str] = set()
        for rec in self.storage.records:
            if not rec.path:
                continue
            if rec.rewritten:
                continue          # hydrated from a finalized previous run
            if rec.path in rewritten_paths:
                rec.rewritten = True   # two records sharing one file (redirect
                continue                # target + direct link): rewrite once
            if not (rec.kind == "page" or (rec.final_url and _is_css(rec.content_type or "", rec.final_url))):
                continue
            try:
                raw = open(self.storage.abs_path(rec.path), "rb").read()
            except OSError:
                continue
            charset = rec.charset or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except (UnicodeError, LookupError):
                text = raw.decode("utf-8", errors="replace")
            base_url = doc_base(rec.final_url or rec.url)
            if rec.kind == "page":
                out = rewrite_html(text, base_url, rec.path, resolver)
                data = out.encode(charset, errors="xmlcharrefreplace")
            else:
                out = rewrite_css(text, base_url, resolver, rec.path)
                data = out.encode(charset, errors="xmlcharrefreplace")
            await self.storage.write(rec.path, data)
            rewritten_paths.add(rec.path)
            rec.rewritten = True

        if self.config.build_search_index:
            await self._build_search_index()

    async def _build_search_index(self) -> None:
        """FTS5 index of every saved page (phase 6). Runs after the rewrite
        pass so the indexed text is the final offline content; hydrated pages
        from previous runs are re-indexed too, so a killed run heals."""
        from app.core.search_index import SearchIndex

        db_path = os.path.join(self.config.output_dir, "search.db")
        index = await asyncio.to_thread(SearchIndex, db_path)
        try:
            indexed: set[str] = set()
            for rec in self.storage.records:
                if rec.kind != "page" or not rec.path or rec.path in indexed:
                    continue
                indexed.add(rec.path)
                path = self.storage.abs_path(rec.path)
                try:
                    raw = open(path, "rb").read()
                except OSError:
                    continue
                text = raw.decode(rec.charset or "utf-8", errors="replace")
                await asyncio.to_thread(
                    index.add_page, rec.final_url or rec.url, rec.path, text)
        finally:
            await asyncio.to_thread(index.close)

    def _write_seed_redirect(self) -> None:
        rec = next((r for r in self.storage.records if r.url == self.seed_norm), None)
        if not rec or not rec.path:
            return  # seed never downloaded successfully; no redirect stub
        if self.storage.needs_seed_redirect(rec.path):
            self.storage.write_seed_redirect(rec.path)

    # -------------------------------------------------------- bookkeeping

    def verify_mirror(self) -> list[BrokenLink]:
        """Public verifier hook — reused later by the GUI's report panel."""
        return verify_links(self)

    async def _record_failure(self, res: FetchResult) -> None:
        kind = "page" if is_page_url(res.final_url or res.url) else "asset"
        # every attempt gets a manifest record (path=None => not saved)
        self.storage.add_record(ResourceRecord(
            url=res.url, kind=kind, path=None,
            final_url=res.final_url or None,
            status=res.status, content_type=res.content_type,
            charset=None, size=0,
        ))
        if res.error == "robots.txt disallowed":
            self.stats.skipped_robots += 1
            self.out_of_scope.add(res.url)
            if self.state:
                await self.state.record_skip(res.url, 0, "robots.txt disallowed")
            return
        if res.error and "exceeds max size" in res.error:
            # oversized file: counted as scope-skip, not a crawl failure
            self.stats.skipped_oversized += 1
            self.out_of_scope.add(res.url)
            if self.state:
                await self.state.record_skip(res.url, 0, res.error)
            return
        page = is_page_url(abs_res_url(res))
        if page:
            self.stats.pages_failed += 1
        else:
            self.stats.assets_failed += 1
        self.failures.append(Failure(
            url=res.url, stage="download",
            error=res.error or "unknown", status=res.status, attempts=res.attempts,
        ))
        if self.on_progress is not None:
            self.on_progress("error", {
                "url": res.url, "error": res.error or "unknown",
                "status": res.status,
            })
        if self.state:
            # the downloader's fetch() already exhausted the in-run retry
            # budget (base delay doubling per attempt); journal the final
            # outcome. Cross-run re-attempts are a phase-3 re-sync concern.
            await self.state.mark_status(
                res.url, "failed",
                error=res.error or "unknown",
                retry_count=res.attempts,
            )


def abs_res_url(res: FetchResult) -> str:
    return res.final_url or res.url


def sniff_charset_of(res: FetchResult) -> str:
    from app.core.downloader import sniff_charset

    return sniff_charset(res.data, res.charset)


def _raw_bytes(text: str, charset: str) -> bytes:
    try:
        return text.encode(charset, errors="xmlcharrefreplace")
    except (UnicodeError, LookupError):
        return text.encode("utf-8", errors="xmlcharrefreplace")


@dataclass
class BrokenLink:
    page: str        # output path of the page containing the reference
    target: str      # normalized target URL
    attr: str        # raw attribute text as found in the html
    reason: str      # "missing-file" | "out-of-scope" | "failed"


# ---------------------------------------------------------------------------
# Offline-link verification: every in-scope reference in every saved page must
# resolve to an existing local file.


def _bad_decode(data: bytes, charset: str) -> tuple[str, str]:
    try:
        return data.decode(charset), charset
    except (UnicodeError, LookupError):
        return data.decode("utf-8", errors="replace"), "utf-8"


def verify_links(crawler: "Crawler") -> list[BrokenLink]:
    """Re-open every saved page and confirm each reference maps to a file that
    actually exists in the mirror.

    The rewriter intentionally leaves some URLs absolute (foreign pages,
    robots-blocked / excluded / depth-capped targets, failed downloads); those
    are *not* skipped here — they are exactly what a broken-link report exists
    to surface."""
    broken: list[BrokenLink] = []
    storage = crawler.storage
    crawler._build_index()
    for rec in storage.records:
        if rec.kind != "page" or not rec.path or rec.status != 200:
            continue
        path = storage.abs_path(rec.path)
        if not _is_file(path):
            continue
        data = open(path, "rb").read()
        text, _ = _bad_decode(data, rec.charset or "utf-8")
        ext = extract_html(text)
        base = doc_base(rec.final_url or rec.url)
        candidates: list[tuple[str, str]] = [(r.url, r.kind) for r in ext.links]
        candidates += [(r.url, r.kind) for r in ext.assets]
        candidates += [(u, "asset") for u in extract_style_urls(text)]
        for raw, kind in candidates:
            if _skippable_attr(raw):
                continue
            absu = absolutize(raw, base)
            if not absu:
                continue
            norm = crawler.normalize_for_site(absu)
            if not norm:
                continue
            final = storage.resolve_redirect(norm)
            target_rec = crawler._target_rec(final)
            if final in crawler.out_of_scope or (
                target_rec is not None and not target_rec.path
            ):
                # robots-blocked / excluded / depth-capped / failed download
                broken.append(BrokenLink(rec.path, final, raw, "out-of-scope"))
                continue
            if not in_scope(
                    (urlsplit(final).netloc or urlsplit(final).hostname or ""),
                    crawler.seed_host, crawler.config.allow_subdomains):
                if kind == "page":
                    continue          # foreign page: intentionally online-only
                # foreign asset: must have been mirrored under its host folder
            rel = crawler._resolve_saved(final)
            if not rel or not _is_file(storage.abs_path(rel)):
                broken.append(BrokenLink(rec.path, final, raw, "missing-file"))
    return broken


def _skippable_attr(raw: str) -> bool:
    u = raw.strip().lower()
    return (not u) or u == "#" or u.startswith((
        "data:", "javascript:", "mailto:", "tel:", "about:", "blob:", "sms:"
    ))


def _is_file(path: str) -> bool:
    import os as _os

    return _os.path.isfile(path)
