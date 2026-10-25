"""CLI entry point for the website-downloader core engine (phase 1).

Usage:
    python -m app.cli <seed_url> --depth 3 --concurrency 8 --output site/
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from app import __version__
from app.core.crawler import Crawler
from app.core.types import (
    DEFAULT_UA,
    CrawlConfig,
)
from app.utils.url_utils import normalize


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="website-downloader",
        description="Mirror a website for offline use (phase 1: core engine).",
    )
    p.add_argument("seed_url", help="URL to start the crawl from")
    p.add_argument("-o", "--output", default=None, help="output directory (default: mirror_<host>)")
    p.add_argument("--depth", type=int, default=3, help="max page-link depth (default 3)")
    p.add_argument("--concurrency", type=int, default=8, help="parallel downloads (default 8)")
    p.add_argument("--per-host-concurrency", type=int, default=4, help="parallel per host (default 4)")
    p.add_argument("--delay", type=float, default=0.2, help="min seconds between requests to a host (politeness)")
    p.add_argument("--timeout", type=float, default=30.0, help="request timeout seconds")
    p.add_argument("--retries", type=int, default=3, help="retry attempts per request")
    p.add_argument("--max-pages", type=int, default=0, help="stop crawling pages after N (0 = unlimited)")
    p.add_argument("--max-file-size", type=float, default=50.0, help="skip files larger than N MB")
    p.add_argument("--user-agent", default=None, help="override the User-Agent header")
    p.add_argument("--ignore-robots", action="store_true",
                   help="ignore robots.txt (mirroring sites you don't own may violate ToS)")
    p.add_argument("--subdomains", action="store_true", help="also crawl seed's subdomains")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip URLs matching glob (repeatable)")
    p.add_argument("--proxy", action="append", default=[], metavar="URL",
                   help="route requests through this proxy (repeatable; http/https/socks5)")
    p.add_argument("--pin-proxy", action="store_true",
                   help="each domain always uses the same proxy")
    p.add_argument("--rotate-ua", action="store_true",
                   help="rotate the User-Agent per request alongside proxies")
    p.add_argument("--proxy-check-url", default="http://www.gstatic.com/generate_204",
                   help="URL used to health-check proxies")
    p.add_argument("--no-verify", action="store_true", help="skip the offline link verification pass")
    p.add_argument("--no-search", action="store_true",
                   help="skip building search.db (the FTS5 page index)")
    p.add_argument("--headless", action="store_true",
                   help="render pages with headless Chromium (JS-heavy sites; "
                        "needs 'playwright install chromium' once)")
    p.add_argument("--cookie", action="append", default=[], metavar="NAME=VALUE",
                   help="send this cookie everywhere (repeatable, or 'a=1; b=2')")
    p.add_argument("--no-db", action="store_true",
                   help="run without project.db (no crash resume)")
    p.add_argument("--fresh", action="store_true",
                   help="delete any existing project.db and start over")
    p.add_argument("--quiet", action="store_true", help="no live progress line")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return p


async def _progress(crawler: Crawler) -> None:
    if not sys.stderr.isatty():
        return
    try:
        while True:
            await asyncio.sleep(0.7)
            st = crawler.stats
            sys.stderr.write(
                f"\rpages {st.pages_saved:5d}ok/{st.pages_failed:3d}fail   "
                f"assets {st.assets_saved:5d}ok/{st.assets_failed:3d}fail   "
                f"{st.bytes_downloaded / 1_000_000:8.2f} MB   "
                f"{st.recent_speed / 1_000_000:5.2f} MB/s".ljust(100) + "\r"
            )
            sys.stderr.flush()
    except asyncio.CancelledError:
        pass


async def _app(cfg: CrawlConfig, quiet: bool) -> int:
    crawler = Crawler(cfg)
    scope = "seed subdomains" if cfg.allow_subdomains else "same host"
    print(f"Mirroring {cfg.seed}")
    print(f"  scope: {scope}    depth={cfg.depth}    concurrency={cfg.concurrency}")
    print(f"  robots.txt: {'ignored' if not cfg.respect_robots else 'respected'}"
          f"    output: {cfg.output_dir}")
    if cfg.state_db:
        print(f"  state: {cfg.state_db}  (resumable)")
    if cfg.proxies:
        print(f"  proxies: {len(cfg.proxies)} configured"
              + ("  (domain-pinned)" if cfg.pin_domain_proxy else ""))
    progress_task = None
    if not quiet:
        progress_task = asyncio.create_task(_progress(crawler))
    try:
        report = await crawler.run()
    finally:
        if progress_task:
            progress_task.cancel()
            await asyncio.gather(progress_task, return_exceptions=True)
    sys.stderr.write("\n")
    print(report.summary_text())
    if report.resumed_pending or report.resumed_done:
        print(f"Resumed: {report.resumed_done} kept from the previous run, "
              f"{report.resumed_pending} requeued")
    print(f"\nManifest: {os.path.join(report.output_dir, 'manifest.json')}")

    if cfg.verify_links:
        broken = crawler.verify_mirror()
        total = len(broken)
        if broken:
            print(f"\nOffline-link verification: {total} non-local reference(s)"
                  " (out-of-scope or failed URLs are left absolute):")
            for bl in broken[:12]:
                print(f"  [{bl.reason}] in {bl.page} -> {bl.target}")
            if total > 12:
                print(f"  ... and {total - 12} more")
        else:
            print("Offline-link verification: OK — every in-scope reference resolves locally.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    seed = normalize(args.seed_url)
    if not seed:
        print(f"error: not a usable http(s) URL: {args.seed_url}", file=sys.stderr)
        return 2
    host = seed.split("://", 1)[-1].split("/", 1)[0]
    default_out = f"mirror_{host.replace(':', '_')}"
    output_dir = args.output or default_out
    state_db = None if args.no_db else os.path.join(output_dir, "project.db")
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(state_db + suffix)
            except OSError:
                pass
    cfg = CrawlConfig(
        seed=seed,
        output_dir=output_dir,
        depth=args.depth,
        concurrency=args.concurrency,
        per_host_concurrency=args.per_host_concurrency,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        max_pages=args.max_pages,
        max_file_size=int(args.max_file_size * 1_000_000),
        user_agent=args.user_agent or DEFAULT_UA,
        respect_robots=not args.ignore_robots,
        allow_subdomains=args.subdomains,
        exclude=tuple(args.exclude),
        verify_links=not args.no_verify,
        build_search_index=not args.no_search,
        state_db=state_db,
        proxies=tuple(args.proxy),
        pin_domain_proxy=args.pin_proxy,
        rotate_user_agent=args.rotate_ua,
        proxy_check_url=args.proxy_check_url,
        headless=args.headless,
        cookies=tuple(args.cookie),
    )
    try:
        return asyncio.run(_app(cfg, quiet=args.quiet))
    except KeyboardInterrupt:
        print("\nInterrupted — progress is saved in project.db.")
        print(f"Re-run the same command to resume: {state_db}")
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
