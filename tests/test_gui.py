"""Phase 4 GUI tests (offscreen): worker signals, pause latency, cancel,
main-window responsiveness while a real crawl runs."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # before any Qt import

import asyncio  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

from app.core.types import CrawlConfig  # noqa: E402
from app.gui.main_window import MainWindow, NewProjectDialog  # noqa: E402
from app.gui.worker import ProjectWorker  # noqa: E402

pytestmark = pytest.mark.usefixtures("qapp")


# ---------------------------------------------------------------- test sites

def build_gui_site(n_pages: int = 120, page_delay: float = 0.0):
    app = web.Application()

    async def index(request):
        links = "".join(f'<a href="/p{i}.html">page {i}</a>' for i in range(n_pages))
        return web.Response(
            text=f"<html><head></head><body><h1>site</h1>{links}</body></html>",
            content_type="text/html")

    async def page(request):
        if page_delay:
            await asyncio.sleep(page_delay)
        return web.Response(
            text="<html><body><img src='/logo.png'></body></html>",
            content_type="text/html")

    async def logo(request):
        return web.Response(body=b"\x89PNG fake", content_type="image/png")

    app.router.add_get("/", index)
    app.router.add_get("/logo.png", logo)
    app.router.add_get("/{tail:.*}", page)
    return app


def make_site(n_pages: int, page_delay: float = 0.0) -> str:
    """Serve a site from its own daemon thread + loop; return the base URL."""
    holder = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def serve():
            runner = web.AppRunner(build_gui_site(n_pages, page_delay))
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            holder["port"] = site._server.sockets[0].getsockname()[1]
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await runner.cleanup()

        loop.run_until_complete(serve())

    threading.Thread(target=run, daemon=True).start()
    while "port" not in holder:
        time.sleep(0.005)
    return f"http://127.0.0.1:{holder['port']}"


@pytest.fixture()
def small_site():
    return make_site(30)


@pytest.fixture()
def slow_site():
    # 150 pages x 40 ms over 4 workers keeps the crawl busy for ~1.5-2 s
    return make_site(150, page_delay=0.04)


# ------------------------------------------------------------------- worker

def _drain(worker: ProjectWorker) -> None:
    """Never leak a live QThread: cancel if needed, then join it."""
    try:
        if worker.isRunning():
            worker.cancel()
    finally:
        worker.wait(15_000)


def test_worker_full_run_signals(qtbot, tmp_path, small_site):
    cfg = CrawlConfig(
        seed=f"{small_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
        state_db=str(tmp_path / "m" / "project.db"),
    )
    worker = ProjectWorker(cfg)
    pages, progress, errors = [], [], []
    worker.page_downloaded.connect(lambda u, p: pages.append((u, p)))
    worker.progress_updated.connect(progress.append)
    worker.error_occurred.connect(errors.append)
    try:
        with qtbot.waitSignal(worker.run_finished, timeout=30_000) as blocker:
            worker.start()
    finally:
        _drain(worker)
    report = blocker.args[0]
    assert report.stats.pages_saved == 31          # index + 30 pages
    assert report.stats.assets_saved >= 1          # logo.png
    assert len(pages) == 31
    assert len(progress) >= 1                       # the ticker ran (fast crawls
    assert progress[0].get("pages_seen", 0) >= 0   # may fit in one tick)
    assert pages[0][1]                             # paths are populated


def test_pause_takes_effect_within_two_seconds(qtbot, tmp_path, slow_site):
    """Acceptance: Pause takes effect within a couple of seconds."""
    cfg = CrawlConfig(
        seed=f"{slow_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
    )
    worker = ProjectWorker(cfg)
    states = []
    worker.state_changed.connect(states.append)
    progress = []
    worker.progress_updated.connect(progress.append)
    try:
        with qtbot.waitSignal(worker.run_finished, timeout=60_000):
            worker.start()
            qtbot.waitUntil(lambda: any(p.get("pages_saved", 0) > 2 for p in progress),
                            timeout=10_000)
            t_pause = time.monotonic()
            worker.pause()
            qtbot.waitUntil(lambda: any(p.get("paused") for p in progress),
                            timeout=2000)
            assert time.monotonic() - t_pause < 2.0, "pause signal not observed in time"
            paused_at = sum(p.get("pages_saved", 0) for p in progress if p.get("paused"))
            worker.resume()
    finally:
        _drain(worker)
    final = max(p.get("pages_saved", 0) for p in progress)
    assert final > paused_at                       # crawl actually continued
    assert "paused" in states and "resumed" in states


def test_cancel_completes_and_keeps_partial_mirror(qtbot, tmp_path, slow_site):
    cfg = CrawlConfig(
        seed=f"{slow_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
        state_db=str(tmp_path / "m" / "project.db"),
    )
    worker = ProjectWorker(cfg)
    progress = []
    worker.progress_updated.connect(progress.append)
    try:
        with qtbot.waitSignal(worker.run_finished, timeout=30_000) as blocker:
            worker.start()
            qtbot.waitUntil(lambda: any(p.get("pages_saved", 0) >= 3 for p in progress),
                            timeout=15_000)
            worker.cancel()
    finally:
        _drain(worker)
    report = blocker.args[0]
    assert report.stats.pages_saved < 150          # genuinely partial
    assert report.stats.pages_saved >= 3


# ------------------------------------------------------------- main window

def test_main_window_stays_responsive(qtbot, tmp_path, small_site):
    """Acceptance: the GUI never freezes while a real crawl runs."""
    win = MainWindow()
    win.show()
    ticks = {"n": 0}
    timer = QTimer(win)
    timer.setInterval(50)
    timer.timeout.connect(lambda: ticks.__setitem__("n", ticks["n"] + 1))
    timer.start()

    cfg = CrawlConfig(
        seed=f"{small_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
    )
    win.start_project(cfg)
    qtbot.waitUntil(lambda: win.worker is not None and not win.worker.isRunning(),
                    timeout=30_000)
    timer.stop()
    _drain(win.worker)
    assert ticks["n"] >= 3                         # main-thread loop stayed alive
    assert win.pages_list.count() == 31           # index + 30 pages
    assert win.labels["state"].text().startswith("Done")
    win.close()


def test_new_project_dialog_validates_seed(qtbot):
    dlg = NewProjectDialog()
    dlg.seed_edit.setText("not a url")
    assert dlg.config() is None
    assert dlg.error_label.text()          # inline, non-blocking feedback
    dlg.seed_edit.setText("https://example.com/")
    cfg = dlg.config()
    assert cfg is not None and cfg.seed == "https://example.com/"


def test_wizard_carries_headless_and_cookies(qtbot, tmp_path):
    from app.core.headless import parse_cookie_header
    from app.gui.login_capture import cookies_to_header

    dlg = NewProjectDialog(default_output=str(tmp_path / "m"))
    dlg.seed_edit.setText("https://example.com/")
    dlg.headless_check.setChecked(True)
    dlg.cookie_edit.setText("session=abc; token=xyz")
    cfg = dlg.config()
    assert cfg is not None
    assert cfg.headless is True
    assert cfg.cookies == ("session=abc; token=xyz",)
    assert parse_cookie_header(cfg.cookies[0]) == [
        ("session", "abc"), ("token", "xyz")]
    assert cookies_to_header([
        {"name": "session", "value": "s3"},
        {"name": "", "value": "dropped"},
    ]) == "session=s3"


# --------------------------------------------------- phase 5: projects + theme

def _make_real_project(qtbot_unused, root: str, name: str, resumable: bool = False):
    """A genuine project folder produced by the engine (not fixtures)."""
    import sqlite3

    from app.core.crawler import Crawler

    base = make_site(5)
    out = os.path.join(root, name)
    os.makedirs(out)
    cfg = CrawlConfig(
        seed=f"{base}/", output_dir=out, depth=1, concurrency=4,
        delay=0.0, retries=0, state_db=os.path.join(out, "project.db"),
    )
    asyncio.run(Crawler(cfg).run())
    if resumable:
        con = sqlite3.connect(os.path.join(out, "project.db"))
        con.execute("UPDATE urls SET status='pending' WHERE url LIKE '%p4.html'")
        con.commit()
        con.close()
    return out


def test_projects_panel_lists_and_resumes(qtbot, tmp_path):
    from app.gui.projects_panel import ProjectsPanel

    root = str(tmp_path / "projects")
    os.makedirs(root)
    _make_real_project(qtbot, root, "site-a", resumable=False)
    _make_real_project(qtbot, root, "site-b", resumable=True)

    panel = ProjectsPanel(root)
    qtbot.addWidget(panel)
    assert panel.table.rowCount() == 2
    names = [panel.table.item(r, 0).text() for r in range(2)]
    statuses = [panel.table.item(r, 2).text() for r in range(2)]
    assert set(names) == {"site-a", "site-b"}
    assert any("Complete" in s for s in statuses)
    assert any("Resumable" in s for s in statuses)

    # select the resumable one and hit Resume -> a ready-to-run config comes out
    row = names.index("site-b")
    panel.table.selectRow(row)
    assert panel.resume_btn.isEnabled()
    got = []
    panel.resume_requested.connect(got.append)
    panel.resume_btn.click()
    assert len(got) == 1
    cfg = got[0]
    assert cfg.output_dir == os.path.join(root, "site-b")
    assert cfg.state_db == os.path.join(root, "site-b", "project.db")

    # double-click opens the saved summary view
    seen = []
    panel.open_summary_requested.connect(seen.append)
    panel.table.selectRow(row)
    panel._double_clicked()
    assert seen and seen[0].name == "site-b"


def test_main_window_saved_summary_view(qtbot, tmp_path):
    win = MainWindow()
    qtbot.addWidget(win)
    root = str(tmp_path / "projects")
    os.makedirs(root)
    _make_real_project(qtbot, root, "site-x")
    win.projects_root = root
    win.projects_panel.projects_root = root
    win.show_projects()
    assert win.stack.currentIndex() == 0
    assert win.projects_panel.table.rowCount() == 1

    win._open_saved_summary(win.projects_panel.summaries[0])
    assert win.stack.currentIndex() == 1
    assert "Viewing saved project" in win.labels["state"].text()
    assert win.progress.value() == 100


def test_theme_toggle_persists(qtbot):
    from PySide6.QtWidgets import QApplication

    win = MainWindow()
    qtbot.addWidget(win)
    win._set_theme("dark")
    assert win.settings.value("theme") == "dark"
    assert win._theme_actions["dark"].isChecked()
    app = QApplication.instance()
    assert bool(app.styleSheet())          # qt-material or the built-in QSS
    win._set_theme("system")
    assert app.styleSheet() == ""
    win.settings.setValue("theme", "system")   # leave clean for other tests


# ------------------------------------------------ phase 6: search + preview

def test_dashboard_search_box(qtbot, tmp_path):
    from app.core.projects import read_summary

    root = str(tmp_path / "projects")
    os.makedirs(root)
    proj = _make_real_project(qtbot, root, "site-s")

    win = MainWindow()
    qtbot.addWidget(win)
    win._open_saved_summary(read_summary(proj))
    assert win.dashboard.current_output == proj

    win.dashboard.search_edit.setText("page")
    win.dashboard._run_search()
    assert win.dashboard.search_results.count() >= 1
    assert win.dashboard._search_hits[0].path
    assert os.path.isfile(os.path.join(proj, *win.dashboard._search_hits[0].path.split("/")))


def test_preview_controller_serves_and_stops(qtbot, tmp_path):
    import urllib.request
    from app.gui.preview import PreviewController

    root = str(tmp_path / "projects")
    os.makedirs(root)
    proj = _make_real_project(qtbot, root, "site-p")

    pc = PreviewController()
    with qtbot.waitSignal(pc.started, timeout=15_000) as blocker:
        pc.open_mirror(proj)
    url = blocker.args[0]
    assert pc.url == url
    html = urllib.request.urlopen(url, timeout=5).read().decode()
    assert "site" in html                       # the mirrored index page
    pc.stop()
