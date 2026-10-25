"""GUI tests for the redesigned shell (offscreen): worker signals, pause
latency, cancel, theme engine, wizard, projects view, toasts, search."""
import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # before any Qt import

import asyncio  # noqa: E402

import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from app.core.projects import read_summary  # noqa: E402
from app.core.types import CrawlConfig  # noqa: E402
from app.gui import theme_engine, widgets  # noqa: E402
from app.gui.main_window import MainWindow  # noqa: E402
from app.gui.new_project_wizard import NewProjectWizard  # noqa: E402
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
    return make_site(150, page_delay=0.04)


def _drain(worker: ProjectWorker) -> None:
    try:
        if worker.isRunning():
            worker.cancel()
    finally:
        worker.wait(15_000)


def _tree_count(tree) -> int:
    total = 0
    stack = [tree.invisibleRootItem()]
    while stack:
        node = stack.pop()
        total += node.childCount()
        for i in range(node.childCount()):
            stack.append(node.child(i))
    return total


# ------------------------------------------------------------- theme engine

def test_theme_engine_tokens_and_qss(qtbot):
    tok = theme_engine.tokens("dark", "violet")
    assert tok["accent"] == theme_engine.ACCENTS["violet"]
    qss = theme_engine.build_qss("midnight", "teal")
    assert theme_engine.ACCENTS["teal"] in qss
    assert "#17171A" in qss or "surface" not in qss      # token values embedded
    # light/dark/midnight/high_contrast all generate valid distinct sheets
    sheets = [theme_engine.build_qss(n) for n in
              ("light", "dark", "midnight", "high_contrast")]
    assert len(set(sheets)) == 4
    assert theme_engine.resolve("system") in ("light", "dark")


def test_theme_switch_persists(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_theme("dark")
    assert win.settings.value("theme") == "dark"
    assert bool(QApplication.instance().styleSheet())    # full QSS applied
    win.set_accent("violet")
    assert win.settings.value("accent") == "violet"
    assert theme_engine.ACCENTS["violet"] in QApplication.instance().styleSheet()
    win.set_theme("system")
    win.set_accent("blue")                               # leave clean


# ------------------------------------------------------------------- worker

def test_worker_full_run_signals(qtbot, tmp_path, small_site):
    cfg = CrawlConfig(
        seed=f"{small_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
        state_db=str(tmp_path / "m" / "project.db"),
    )
    worker = ProjectWorker(cfg)
    pages, progress, tree_events = [], [], []
    worker.page_downloaded.connect(lambda u, p: pages.append((u, p)))
    worker.page_event.connect(lambda d: tree_events.append(d))
    worker.progress_updated.connect(progress.append)
    try:
        with qtbot.waitSignal(worker.run_finished, timeout=30_000) as blocker:
            worker.start()
    finally:
        _drain(worker)
    report = blocker.args[0]
    assert report.stats.pages_saved == 31                # index + 30 pages
    assert report.stats.assets_saved >= 1               # logo.png
    assert len(pages) == 31
    assert len(tree_events) == 31
    assert all("referer" in d for d in tree_events)     # tree payload complete
    assert len(progress) >= 1


def test_pause_takes_effect_within_two_seconds(qtbot, tmp_path, slow_site):
    """Acceptance: Pause takes effect within a couple of seconds."""
    cfg = CrawlConfig(
        seed=f"{slow_site}/", output_dir=str(tmp_path / "m"), depth=1,
        concurrency=4, delay=0.0, retries=0,
    )
    worker = ProjectWorker(cfg)
    states, progress = [], []
    worker.state_changed.connect(states.append)
    worker.progress_updated.connect(progress.append)
    try:
        with qtbot.waitSignal(worker.run_finished, timeout=60_000):
            worker.start()
            qtbot.waitUntil(
                lambda: any(p.get("pages_saved", 0) > 2 for p in progress),
                timeout=10_000)
            t_pause = time.monotonic()
            worker.pause()
            qtbot.waitUntil(lambda: any(p.get("paused") for p in progress),
                            timeout=2000)
            assert time.monotonic() - t_pause < 2.0, "pause not observed in time"
            paused_at = sum(p.get("pages_saved", 0) for p in progress
                            if p.get("paused"))
            worker.resume()
    finally:
        _drain(worker)
    final = max(p.get("pages_saved", 0) for p in progress)
    assert final > paused_at                            # crawl actually continued
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
            qtbot.waitUntil(
                lambda: any(p.get("pages_saved", 0) >= 3 for p in progress),
                timeout=15_000)
            worker.cancel()
    finally:
        _drain(worker)
    report = blocker.args[0]
    assert 3 <= report.stats.pages_saved < 150          # genuinely partial


# --------------------------------------------------------------- main window

def test_main_window_stays_responsive(qtbot, tmp_path, small_site):
    """Acceptance: the GUI never freezes while a real crawl runs."""
    win = MainWindow()
    win.show()
    qtbot.addWidget(win)
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
    assert ticks["n"] >= 3                             # main-thread loop alive
    assert win.stack.currentIndex() == 1                # dashboard visible
    assert win.labels["state"].text() in ("Completed", "Done")
    assert _tree_count(win.pages_list) == 31            # site-map tree filled
    win.close()


def test_dashboard_search_box(qtbot, tmp_path):
    from app.gui.live_dashboard import LiveDashboard

    root = str(tmp_path / "projects")
    os.makedirs(root)
    base = make_site(5)
    out = os.path.join(root, "site-s")
    os.makedirs(out)

    from app.core.crawler import Crawler

    asyncio.run(Crawler(CrawlConfig(
        seed=f"{base}/", output_dir=out, depth=1, concurrency=4,
        delay=0.0, retries=0, state_db=os.path.join(out, "project.db"),
    )).run())

    dash = LiveDashboard()
    qtbot.addWidget(dash)
    dash.show_project_summary(read_summary(out))
    assert dash.current_output == out
    dash.search_edit.setText("page")
    dash._run_search()
    assert dash.search_results.count() >= 1
    assert os.path.isfile(os.path.join(
        out, *dash._search_hits[0].path.split("/")))


def test_toast_lifecycle(qtbot):
    host_parent = MainWindow()
    qtbot.addWidget(host_parent)
    host = host_parent.toasts
    t = host.toast("Saved!", "success", msec=300)
    assert t is not None
    qtbot.waitUntil(lambda: t.parent() is None or not t.isVisible(),
                    timeout=3000)


# -------------------------------------------------------------------- wizard

def test_wizard_steps_and_config(qtbot, tmp_path):
    wiz = NewProjectWizard(str(tmp_path), seed_url="https://example.com/")
    qtbot.addWidget(wiz)
    assert wiz.stack.count() == 6
    assert wiz.next.text() == "Next"

    # auto-name follows the seed until the user types their own
    wiz.name_edit._touched = True
    wiz.name_edit.setText("my-name")
    wiz.seed_edit.setText("https://other.example.com/docs/")
    assert wiz.name_edit.text() == "my-name"
    wiz.name_edit._touched = False
    wiz._auto_name()
    assert wiz.name_edit.text() == "other-example-com"

    wiz._pick_profile("Docs site")
    cfg = wiz.config()
    assert cfg is not None
    assert cfg.seed == "https://other.example.com/docs/"
    assert cfg.depth == 6 and cfg.allow_subdomains is True

    wiz._pick_profile("Custom")
    wiz.depth_spin.setValue(2)
    wiz.scope.set_current(1)
    wiz.exclude_edit.setPlainText("*print=*\n")
    wiz.headless_check.setChecked(True)
    wiz.cookie_edit.setText("session=abc; token=xyz")
    cfg = wiz.config()
    assert cfg.depth == 2 and cfg.allow_subdomains is True
    assert cfg.exclude == ("*print=*",)
    assert cfg.headless and cfg.cookies == ("session=abc; token=xyz",)

    # slide navigation + dots + Start label
    wiz._slide_to(5)
    assert wiz.next.text() == "Start"


def test_wizard_rejects_bad_seed(qtbot, tmp_path):
    wiz = NewProjectWizard(str(tmp_path))
    qtbot.addWidget(wiz)
    wiz.seed_edit.setText("not a url")
    assert wiz.config() is None
    assert not wiz._validate_seed()


# ------------------------------------------------------------- projects view

def test_projects_view_cards_filters_resume(qtbot, tmp_path):
    from app.gui.projects_view import ProjectsView

    root = str(tmp_path / "projects")
    os.makedirs(root)
    for name, resumable in (("site-a", False), ("site-b", True)):
        base = make_site(5)
        out = os.path.join(root, name)
        os.makedirs(out)
        from app.core.crawler import Crawler
        import sqlite3

        asyncio.run(Crawler(CrawlConfig(
            seed=f"{base}/", output_dir=out, depth=1, concurrency=4,
            delay=0.0, retries=0, state_db=os.path.join(out, "project.db"),
        )).run())
        if resumable:
            con = sqlite3.connect(os.path.join(out, "project.db"))
            con.execute("UPDATE urls SET status='pending' WHERE url LIKE '%p4.html'")
            con.commit()
            con.close()

    view = ProjectsView(root)
    qtbot.addWidget(view)
    view.refresh()
    cards = view.visible_cards()
    assert len(cards) == 2
    assert {c.summary.name for c in cards} == {"site-a", "site-b"}

    # status chips filter correctly
    view._pick_chip("completed")
    assert [c.summary.name for c in view.visible_cards()] == ["site-a"]
    view._pick_chip("running")
    assert [c.summary.name for c in view.visible_cards()] == ["site-b"]
    view._pick_chip("all")

    # inline text filter
    view.search.setText("site-a")
    assert [c.summary.name for c in view.visible_cards()] == ["site-a"]
    view.search.setText("")

    # resume emits a ready-to-run config rebuilt from the project's DB
    got = []
    view.resume_requested.connect(got.append)
    target = next(c for c in view.visible_cards() if c.summary.name == "site-b")
    target.resume_btn.click()
    assert len(got) == 1 and got[0].output_dir == os.path.join(root, "site-b")


def test_projects_view_empty_state(qtbot, tmp_path):
    from app.gui.projects_view import ProjectsView

    view = ProjectsView(str(tmp_path / "none"))
    qtbot.addWidget(view)
    view.refresh()
    assert not view.visible_cards()                    # empty state showing
    view.search.setText("zzz")                        # no-matches state also fine
    view.refresh()


# ------------------------------------------------------------- preview (kept)

def test_preview_controller_serves_and_stops(qtbot, tmp_path):
    import urllib.request
    from app.gui.preview import PreviewController

    root = str(tmp_path / "projects")
    os.makedirs(root)
    base = make_site(5)
    proj = os.path.join(root, "site-p")
    os.makedirs(proj)
    from app.core.crawler import Crawler

    asyncio.run(Crawler(CrawlConfig(
        seed=f"{base}/", output_dir=proj, depth=1, concurrency=4,
        delay=0.0, retries=0, state_db=os.path.join(proj, "project.db"),
    )).run())

    pc = PreviewController()
    with qtbot.waitSignal(pc.started, timeout=15_000) as blocker:
        pc.open_mirror(proj)
    url = blocker.args[0]
    html = urllib.request.urlopen(url, timeout=5).read().decode()
    assert "site" in html
    pc.stop()
