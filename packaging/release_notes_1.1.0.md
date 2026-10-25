# Website Downloader 1.1.0

The redesigned interface release: token-driven theming, native-feel motion, and pro-tool ergonomics — on top of the resumable mirroring engine introduced in 1.0.0.

## Highlights

### Interface, rebuilt
- **Theme engine** — every color, radius, spacing and animation duration is a *token*; stylesheets are generated per theme. **Light / Dark / Midnight / High-contrast / System** themes switch instantly (no restart) and persist, plus an **accent-color picker** (blue / teal / violet / coral / green) that restyles any theme. The OS "reduce animations" accessibility setting collapses all motion to under 50 ms.
- **Motion that feels native** — cross-faded page transitions, sliding wizard steps with animated dot indicator, count-up stat numbers, animated progress fills, card entrance fades, a pulsing Running pill, toasts with countdown bars — one shared `animate()` helper, 150–250 ms, `OutCubic`.
- **Six-step New Project wizard** — Seed → Profile (Blog / Docs / E-commerce / Single page / Custom presets) → Scope → Network (live-readout sliders) → Output → Review, with horizontal slide transitions.
- **Pro-tool ergonomics** — command palette (**Ctrl+K**), global shortcuts (Ctrl+N / Ctrl+R / Ctrl+, / Space / F1), system tray with an active-download badge and native completion notifications, **close-to-tray mode** (the X hides to the tray while crawls keep running; tray-Quit exits for real), drag-and-drop a URL straight from the browser, right-click context menus (Resume / Open / Export .zip / Rename / **Duplicate settings** / Delete), skeleton-loading shimmer, designed empty & error states, auto-saved window state.

### New in this release
- **Project Details page** — double-click any project to open its saved crawl as an interactive **page tree built from the `link_edges` graph** in `project.db` (double-click a node to open the saved file), with stats, Resume, browser preview, and one-click **Export as .zip**.
- **Duplicate settings into a new project** — the wizard comes up prefilled from the saved project's exact configuration, output folder fresh.
- **Live dashboard** — count-up stat cards (pages / assets / speed / ETA), a rolling **speed-history sparkline** in the active accent, a live site-map tree, and a color-coded, filterable, auto-scrolling activity log with **export-to-file**.
- **Interaction hardening** — dropped custom frameless hit-testing (mis-mapped regions swallowed clicks as window drags) in favor of the native window frame with a styled in-app header; every interactive surface is now covered by a real mouse-click regression test. Also fixed the sidebar collapse button that could never collapse by click.

## Install

- **Windows**: download `WebsiteDownloader-1.1.0-win64.exe` below — one file, no Python needed. GUI on launch; scripted crawls via `WebsiteDownloader.exe --cli <url> --depth 2 -o mirror`.
- Headless-browser crawling (JS-heavy sites) is optional and needs a one-time `playwright install chromium` — the app prompts when you first enable it.
- An Inno Setup installer script ships in `packaging/` for those who want Start Menu shortcuts and an uninstaller.

## From source

```
git clone https://github.com/cyber-attacker/website-downloader
cd website-downloader
pip install -r requirements.txt
python -m app.gui        # desktop app
python -m app.cli <url>  # CLI (same engine)
```

106 tests: kill-and-resume byte-equivalence, exactly-once fetching, mock-proxy rotation, offscreen Qt click coverage, FTS5 + preview server, and headless-vs-plain-HTTP acceptance.

**Full changelog:** `CHANGELOG.md` in the repo.
