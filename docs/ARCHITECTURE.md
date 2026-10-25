# Architecture

One engine, two shells (GUI and CLI). The engine never imports Qt or argparse —
`Crawler` takes a frozen `CrawlConfig` and returns a `CrawlReport`.

```
                    ┌────────────┐   ┌────────────┐
                    │  app.gui   │   │  app.cli   │
                    │ (PySide6)  │   │ (argparse) │
                    └─────┬──────┘   └─────┬──────┘
                          │     CrawlConfig │
                          ▼                ▼
        ┌────────────────────────────────────────────┐
        │                Crawler.run()               │
        │  queue → workers → _process → finalize     │
        └───┬────────┬─────────┬────────┬────────┬────┘
            ▼        ▼         ▼        ▼        ▼
        Downloader Rewriter  Storage  StateDB  SearchIndex
        (aiohttp)  (lexbor)  (files)  (SQLite) (FTS5)
```

## The crawl pipeline

1. **Crawl pass.** `concurrency` asyncio workers pull normalized URLs off a
   queue. Each fetch: robots gate → per-host politeness → optional proxy
   acquisition (rotated pool) → GET (or headless render for page jobs).
   Responses are saved **raw** immediately; discoveries are journaled to
   SQLite as `pending` *before* the row flips to `done` (crash window = one
   re-fetch, never a lost branch).

2. **Finalize pass.** After `queue.join()`, every saved HTML/CSS file is
   re-read and rewritten using the complete redirect map. Records' saved
   paths are the single source of truth (`_build_index` / `_resolve_saved`),
   so content-driven saving (`/css?query` serving `text/css`) and the rewrite
   pass can never disagree. Files are indexed into the FTS5 search db here.

3. **Verify pass** (optional) re-opens every saved page and re-resolves each
   reference — absolute attrs are *not* skipped, because intentionally
   absolute links (out-of-scope, failed) are exactly what the report exists
   to surface.

## Key invariants

- **Scope** = the seed's netloc (`host:port`), `www.`-stripped. Foreign
  *assets* are mirrored under a `host/` folder; foreign *pages* stay absolute.
- **Normalize once, use everywhere.** `app/utils/url_utils.normalize()` keeps
  the trailing-slash directory marker; `doc_base()` restores browser join
  semantics for parent-relative references — this combination is what keeps
  `../../../x` rewrites exact.
- **Left-absolute rule.** Anything not in the mirror (robots, depth, exclude,
  max-pages, oversized, failed download, foreign page) is rewritten to its
  absolute URL rather than a broken relative path.
- **Crash safety.** Every state transition is its own small SQLite
  transaction (WAL). On resume: `pending`/`downloading` requeue, `done`
  files stay on disk, `skipped`/`failed` restore leave-absolute semantics,
  and a persistent `finalized` flag decides whether hydrated files may be
  trusted or must be re-finalized.
- **Two records may share one output file** (redirect target + direct link):
  finalization is deduped by path and double-rewrite byte drift is impossible.

## Module map

| Module | Responsibility |
|---|---|
| `app/core/crawler.py` | Orchestrator: BFS queue, scope gates, redirect map, finalize, verify, pause/cancel |
| `app/core/downloader.py` | aiohttp pool, exponential backoff, robots gate, proxy rotation, size caps |
| `app/core/rewriter.py` | lexbor HTML/CSS extraction + attribute/srcset/style rewriting |
| `app/core/storage.py` | URL→path mapping, atomic writes, manifest |
| `app/core/state_db.py` | The resumable queue (`urls` / `project_meta` / `link_edges`) |
| `app/core/proxy_manager.py` | Health checks, round-robin, pinning, UA pool |
| `app/core/preview_server.py` | Local HTTP server for finished mirrors |
| `app/core/search_index.py` | FTS5 index + snippet queries |
| `app/core/headless.py` | Playwright page rendering → `FetchResult` |
| `app/core/projects.py` | Project summaries from any `project.db` (GUI-agnostic) |
| `app/utils/url_utils.py` | Pure functions: normalize, scope, classify, map to paths |
| `app/gui/*` | Qt shells over the same engine (worker QThread, panels, theme) |

## GUI threading model

`ProjectWorker(QThread)` runs `asyncio.run(Crawler...)` on its own loop and
emits Qt signals (auto-queued to the GUI thread). GUI→engine control travels
the other way through `loop.call_soon_threadsafe`, so the engine's
`asyncio.Event` / cancel flags are only ever mutated inside the loop. The GUI
never blocks: pause takes effect between in-flight requests and is observed
via the 400 ms progress ticker.
