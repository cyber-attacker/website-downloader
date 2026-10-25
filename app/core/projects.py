"""Project store: every folder under the projects root holding a project.db
is a project. Summaries are read straight from each project's SQLite state —
the same single source of truth the crawler writes (GUI-agnostic, testable
without Qt)."""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

from app.core.types import CrawlConfig

PROJECT_DB = "project.db"


@dataclass
class ProjectSummary:
    folder: str
    name: str
    seed: str = ""
    depth: int = 0
    concurrency: int = 0
    output_dir: str = ""
    status: str = "empty"            # complete | incomplete | empty
    progress_pct: int = 0
    pages_done: int = 0
    pages_failed: int = 0
    assets_done: int = 0
    urls_total: int = 0
    created_at: float | None = None
    last_run_at: float | None = None

    @property
    def status_display(self) -> str:
        return {
            "complete": "Complete",
            "incomplete": "Resumable",
            "empty": "No data",
        }.get(self.status, self.status.title())


def _connect_ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.replace(os.sep, '/')}?mode=ro",
                           uri=True, timeout=3.0)


def read_summary(folder: str) -> ProjectSummary | None:
    """Read one project folder; None when it holds no project.db."""
    db_path = os.path.join(folder, PROJECT_DB)
    if not os.path.isfile(db_path):
        return None
    summary = ProjectSummary(folder=folder, name=os.path.basename(folder) or folder,
                             output_dir=folder)
    try:
        con = _connect_ro(db_path)
        try:
            cur = con.cursor()
            meta: dict[str, object] = {}
            for key, value in cur.execute("SELECT key, value FROM project_meta"):
                try:
                    import json
                    meta[key] = json.loads(value)
                except Exception:                     # noqa: BLE001
                    meta[key] = value
            counts: dict[str, int] = {}
            for status, n in cur.execute(
                    "SELECT status, COUNT(*) FROM urls GROUP BY status"):
                counts[status] = n

            summary.seed = str(meta.get("seed_url", ""))
            summary.depth = int(meta.get("max_depth", 0) or 0)
            summary.concurrency = int(meta.get("concurrency", 0) or 0)
            summary.created_at = float(meta.get("created_at") or 0) or None
            summary.last_run_at = float(meta.get("last_run_at") or 0) or None

            done = counts.get("done", 0)
            pending = counts.get("pending", 0) + counts.get("downloading", 0)
            failed = counts.get("failed", 0)
            skipped = counts.get("skipped", 0)
            total = done + pending + failed + skipped
            summary.urls_total = total
            cur.execute("SELECT COUNT(*) FROM urls WHERE status='done' "
                        "AND content_type LIKE '%text/html%'")
            summary.pages_done = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM urls WHERE status='failed' "
                        "AND content_type LIKE '%text/html%'")
            summary.pages_failed = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM urls WHERE status='done' "
                        "AND (content_type IS NULL OR content_type NOT LIKE '%text/html%')")
            summary.assets_done = cur.fetchone()[0]

            if total == 0:
                summary.status = "empty"
                summary.progress_pct = 0
            elif pending > 0:
                summary.status = "incomplete"
                summary.progress_pct = int(done * 100 / total)
            else:
                summary.status = "complete"
                summary.progress_pct = 100 if done else 0
        finally:
            con.close()
    except sqlite3.Error:
        pass                          # unreadable db: show as empty, never crash the panel
    return summary


def scan_projects(projects_root: str) -> list[ProjectSummary]:
    """Every subfolder with a project.db, newest run first."""
    if not os.path.isdir(projects_root):
        return []
    out: list[ProjectSummary] = []
    for name in os.listdir(projects_root):
        folder = os.path.join(projects_root, name)
        if not os.path.isdir(folder):
            continue
        summary = read_summary(folder)
        if summary is not None:
            out.append(summary)
    out.sort(key=lambda s: (-(s.last_run_at or 0), s.name.lower()))
    return out


def config_for(summary: ProjectSummary) -> CrawlConfig:
    """Rebuild the exact CrawlConfig a project was started with, so Resume
    reuses its original settings (build guide, phase 5)."""
    import json

    db_path = os.path.join(summary.folder, PROJECT_DB)
    exclude: tuple[str, ...] = ()
    respect_robots = True
    allow_subdomains = False
    try:
        con = _connect_ro(db_path)
        try:
            meta = dict(con.cursor().execute("SELECT key, value FROM project_meta"))
            raw_excl = meta.get("exclude", "[]")
            try:
                exclude = tuple(json.loads(raw_excl))
            except Exception:                      # noqa: BLE001
                exclude = ()
            respect_robots = bool(meta.get("respect_robots", 1))
            allow_subdomains = bool(meta.get("allow_subdomains", 0))
        finally:
            con.close()
    except sqlite3.Error:
        pass
    seed = summary.seed or f"file://{summary.folder}"   # unreachable; validated upstream
    return CrawlConfig(
        seed=seed,
        output_dir=summary.folder,
        depth=summary.depth,
        concurrency=summary.concurrency or 8,
        respect_robots=respect_robots,
        allow_subdomains=allow_subdomains,
        exclude=exclude,
        state_db=db_path,
    )
