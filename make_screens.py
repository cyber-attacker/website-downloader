"""Generate README screenshots of the redesigned UI (offscreen grabs)."""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from PySide6.QtWidgets import QApplication

from app.gui import theme_engine

app = QApplication([])
theme_engine.apply(app, "dark", "violet")

from test_gui import make_site  # noqa: E402

from app.core.crawler import Crawler  # noqa: E402
from app.core.projects import read_summary  # noqa: E402
from app.core.types import CrawlConfig  # noqa: E402
from app.gui.main_window import MainWindow  # noqa: E402
from app.gui.new_project_wizard import NewProjectWizard  # noqa: E402

OUT = os.path.join("docs", "img")
os.makedirs(OUT, exist_ok=True)

# ---- a real demo project so the cards/tree hold live data
root = os.path.join(tempfile.mkdtemp(), "projects")
os.makedirs(root)
for name, n, resumable in (("docs-portal", 5, False), ("news-blog", 5, True)):
    base = make_site(n)
    out = os.path.join(root, name)
    os.makedirs(out)
    asyncio.run(Crawler(CrawlConfig(
        seed=f"{base}/", output_dir=out, depth=1, concurrency=4,
        delay=0.0, retries=0, state_db=os.path.join(out, "project.db"),
    )).run())
    if resumable:
        import sqlite3
        con = sqlite3.connect(os.path.join(out, "project.db"))
        con.execute("UPDATE urls SET status='pending' WHERE url LIKE '%p4.html'")
        con.commit()
        con.close()

def settle(ms: int = 600):
    """Let animations finish before grabbing (cross-fades start at opacity 0)."""
    import time
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


win = MainWindow()
win.resize(1100, 720)
win.projects_root = root
win.projects_view.projects_root = root
win.settings_view.root_edit.setText(root)
win.show()
app.processEvents()

# 1 — projects dashboard (dark)
win.projects_view.refresh()
settle()
win.grab().save(os.path.join(OUT, "projects_dark.png"))
print("saved projects_dark.png")

# 2 — live dashboard showing a saved project
win._open_saved_summary(read_summary(os.path.join(root, "docs-portal")))
settle()
win.grab().save(os.path.join(OUT, "dashboard_dark.png"))
print("saved dashboard_dark.png")

# 3 — settings
win._goto(2)
settle()
win.grab().save(os.path.join(OUT, "settings_dark.png"))
print("saved settings_dark.png")

# 4 — wizard on the profile step
wiz = NewProjectWizard(root, seed_url="https://example.com/")
wiz.resize(640, 520)
wiz.show()
wiz._slide_to(1)
wiz._pick_profile("Docs site")
settle()
wiz.grab().save(os.path.join(OUT, "wizard_dark.png"))
print("saved wizard_dark.png")

# 5 — light theme projects (the theme system sells itself)
theme_engine.apply(app, "light", "blue")
win.set_theme("light")
win.show_projects()
win.projects_view.refresh()
settle()
win.grab().save(os.path.join(OUT, "projects_light.png"))
print("saved projects_light.png")

# 6 — project details page (dark again, from the demo project)
theme_engine.apply(app, "dark", "violet")
win.set_theme("dark")
win._open_saved_summary(read_summary(os.path.join(root, "docs-portal")))
settle()
win.grab().save(os.path.join(OUT, "details_dark.png"))
print("saved details_dark.png")

win.close()
app.processEvents()
print("done")
