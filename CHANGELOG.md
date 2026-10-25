# Changelog

## 1.0.0 — 2026-10-25

First complete release: all eight build phases of the master plan, done.

### Core engine
- Async crawler (aiohttp) with configurable concurrency, per-host limits and
  per-host politeness delay; robots.txt respected by default.
- Two-pass offline rewriting: raw-first save, then a finalize pass with the
  complete redirect map (links through redirects resolve correctly).
- Content-driven routing: URLs served as CSS are always rewritten and saved
  with a `.css` extension, regardless of the URL's shape.
- Out-of-scope / robots-blocked / excluded / failed URLs are left as absolute
  URLs so offline pages degrade gracefully, never 404 against the mirror.
- Manifest (`manifest.json`) with per-URL records, redirects, failures.

### Persistence & resume (phase 2)
- `project.db` (SQLite, WAL): `urls` queue + history, `project_meta`,
  `link_edges` graph.
- Kill the process anywhere → re-run resumes; automated byte-equivalence test
  against an uninterrupted reference run.
- Exactly-once fetching enforced and tested.

### Proxy manager (phase 3)
- HTTP/HTTPS/SOCKS5 pool with startup health checks, round-robin rotation,
  optional per-domain pinning, failure cooldowns.
- 403/429/reset through a proxy rotates to another proxy before failing.
- User-Agent rotation per request.

### GUI (phases 4–5)
- PySide6 shell: New Project wizard, live dashboard with Pause/Resume/Cancel
  (pause observed < 2 s, tested), project worker on a QThread with queued
  signals.
- Projects dashboard reading every project's SQLite state, one-click Resume,
  saved-project summary view, dark/light/system theming.

### Preview & search (phase 6)
- Local preview server: correct MIME types, directory → index.html, traversal
  guard.
- FTS5 full-text index over every saved page, built during finalize,
  resume-safe; GUI search box.

### Advanced crawling (phase 7)
- Optional headless-Chromium page rendering (Playwright) — JS-injected content
  and links captured (acceptance-tested vs plain HTTP).
- Cookie passthrough + visible-browser "Log in first" capture.

### Packaging (phase 8)
- PyInstaller onefile windowed exe with `--cli` passthrough; frozen-windowed
  teardown fixes (NUL streams, direct exit).
- Inno Setup installer script; Playwright browsers intentionally not bundled.

### Tests
- 96 tests: units, integration against local aiohttp sites, kill/resume
  equivalence, mock-proxy rotation, offscreen Qt, preview/FTS5, headless.
