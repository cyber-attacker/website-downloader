# Changelog

## 1.1.0 — 2026-10-25

The redesigned interface release: token-driven theming, motion, pro-tool
ergonomics — plus interaction hardening and new features.

### Interface redesign
- Theme engine: every color/radius/duration is a token; QSS generated per
  theme. Light / Dark / Midnight / High-contrast / System, instant live
  switching, accent-color picker (blue/teal/violet/coral/green). OS
  "reduce animations" collapses all motion to <50 ms.
- Motion system: one shared `animate()` helper — cross-faded page
  transitions, sliding wizard steps with animated dot indicator, count-up
  stat numbers, animated progress fills, card entrance fades, pulsing
  Running pill, toasts with countdown bars. All 150–250 ms, OutCubic.
- Six-step New Project wizard with profile presets (Blog / Docs /
  E-commerce / Single page / Custom) and live-readout sliders.
- Live dashboard: count-up stat cards (pages / assets / speed / ETA),
  rolling speed-history sparkline, live site-map tree, color-coded
  filterable auto-scrolling log with export-to-file.
- Command palette (Ctrl+K), global shortcuts (Ctrl+N/R/,/Space, F1 help),
  system tray with active-download badge and native completion
  notifications, drag-and-drop URL intake, skeleton-loading shimmer,
  designed empty/error states, first-run tips, auto-saved window state.

### Interaction fixes
- Dropped the custom frameless hit-testing (mis-mapped regions swallowed
  clicks as window drags): standard native window frame — every click
  lands; branding lives in a styled in-app header strip.
- Sidebar collapse button never collapsed via click (signal passed its
  checked state into the toggle); fixed and pinned by a real-click
  regression test over every interactive surface.

### New features
- Project Details page: double-click a project → its saved crawl as an
  interactive page tree built from the `link_edges` graph in project.db;
  Resume / Open folder / browser preview / Export as .zip; double-click a
  tree node opens the saved file.
- Duplicate settings into a new project: wizard prefilled from the saved
  project's config with a fresh output folder.
- Close-to-tray mode: the X hides to the tray while crawls keep running;
  tray Quit is the real exit.
- `run_gui.py` is a self-bootstrapping launcher: re-executes through .venv
  (pythonw), offers a one-time venv bootstrap on first run, routes
  --cli/--help to the console.

### Engine
- Page events now carry the discovery referer (site-map tree feed).

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
