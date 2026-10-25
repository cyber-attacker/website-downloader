"""Main window shell (spec §4/§5): standard native window frame (the spec
explicitly allows "styled standard title bar" — and it guarantees every click
lands), persistent sidebar, cross-fade page transitions, toast host, command
palette (Ctrl+K), global shortcuts, system tray with an active-download
badge, drag-and-drop URL intake, status bar, first-run tips, saved state."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core.projects import ProjectSummary
from app.core.types import CrawlConfig
from app.gui import anim, theme_engine, widgets
from app.gui.live_dashboard import LiveDashboard
from app.gui.new_project_wizard import NewProjectWizard
from app.gui.projects_view import ProjectsView
from app.gui.settings_view import SettingsView
from app.gui.sidebar import Sidebar
from app.gui.worker import ProjectWorker

ORG = "WebsiteDownloader"
APP = "Website Downloader"

THEME_CYCLE = ["system", "light", "dark", "midnight", "high_contrast"]


def app_icon() -> QIcon:
    """Branded mark: accent rounded square + white download glyph."""
    tok = theme_engine.tokens()
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(tok["accent"]))
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setBrush(QColor("#FFFFFF"))
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint, QRectF
    p.drawRect(QRectF(29, 16, 6, 20))
    p.drawPolygon(QPolygon([QPoint(22, 32), QPoint(42, 32), QPoint(32, 46)]))
    p.drawRect(QRectF(20, 50, 24, 5))
    p.end()
    return QIcon(pm)


class HeaderBar(QFrame):
    """Slim in-app strip under the native title bar: brand + quick actions.
    Pure client widgets — every pixel is normally clickable."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("HeaderBar")
        self.setFixedHeight(48)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 6, 10, 6)
        lay.setSpacing(10)
        self.icon_lbl = QLabel()
        self.icon_lbl.setPixmap(app_icon().pixmap(20, 20))
        lay.addWidget(self.icon_lbl)
        self.brand = QLabel("Website Downloader")
        f = QFont()
        f.setPointSizeF(12.5)
        f.setBold(True)
        self.brand.setFont(f)
        lay.addWidget(self.brand)
        lay.addStretch(1)

        self.theme_btn = widgets.icon_btn(
            "moon", f"Theme: system — click to cycle ({' / '.join(THEME_CYCLE)})")
        self.preview_btn = widgets.icon_btn("globe", "Preview the current mirror in your browser")
        self.new_btn = QPushButton("New Project")
        self.new_btn.setProperty("variant", "primary")
        for b in (self.theme_btn, self.preview_btn, self.new_btn):
            lay.addWidget(b)

    def refresh_theme(self, theme_name: str):
        self.icon_lbl.setPixmap(app_icon().pixmap(20, 20))
        glyph = {"light": "sun", "dark": "moon", "midnight": "moon",
                 "high_contrast": "adjust", "system": "desktop"}[theme_name]
        tok = theme_engine.tokens()
        self.theme_btn.setIcon(widgets.icon(glyph, tok["text2"]))
        self.theme_btn.setToolTip(
            f"Theme: {theme_name} — click to cycle ({' / '.join(THEME_CYCLE)})")


class CommandPalette(QDialog):
    """Ctrl+K overlay (spec §5): type to jump to projects, screens, actions."""

    triggered = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.setModal(True)
        self.setFixedWidth(520)
        self.entries: list[tuple[str, str, object]] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a project or command…  (Esc to close)")
        self.edit.textChanged.connect(self._refill)
        lay.addWidget(self.edit)
        self.listw = QListWidget()
        self.listw.itemActivated.connect(self._activate)
        self.listw.itemClicked.connect(self._activate)
        lay.addWidget(self.listw)

    def open_with(self, entries: list[tuple[str, str, object]]):
        self.entries = entries
        self.edit.clear()
        self._refill("")
        self.show()
        self.edit.setFocus()

    def _refill(self, text: str):
        text = text.strip().lower()
        self.listw.clear()
        for title, subtitle, payload in self.entries:
            hay = (title + " " + subtitle).lower()
            if not text or text in hay:
                item = QListWidgetItem(f"{title}   ·   {subtitle}")
                item.setData(Qt.UserRole, payload)
                self.listw.addItem(item)

    def _activate(self, item):
        payload = item.data(Qt.UserRole)
        self.accept()
        self.triggered.emit(payload)


class TipCard(widgets.Card):
    """Non-blocking first-run tips pinned to a corner (spec §5 onboarding,
    without a full-window veil that would swallow clicks)."""

    dismissed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 12)
        lay.setSpacing(6)
        head_row = QHBoxLayout()
        t = QLabel("Welcome — quick tips")
        t.setProperty("cardtitle", True)
        head_row.addWidget(t)
        head_row.addStretch(1)
        close = widgets.icon_btn("times", "Dismiss (you can always press Ctrl+K)")
        close.clicked.connect(self._dismiss)
        head_row.addWidget(close)
        lay.addLayout(head_row)
        for line in (
            "•  New Project in the sidebar — paste a URL, or drag one in from your browser.",
            "•  Pause / resume / cancel live; killed crawls resume exactly where they stopped.",
            "•  Ctrl+K opens the command palette; themes live in Settings."):
            lab = QLabel(line)
            lab.setWordWrap(True)
            lay.addWidget(lab)
        got = QPushButton("Got it")
        got.setProperty("variant", "primary")
        got.clicked.connect(self._dismiss)
        lay.addWidget(got, 0, Qt.AlignRight)
        self.setFixedWidth(360)
        self.adjustSize()

    def _dismiss(self):
        self.dismissed.emit()
        self.hide()
        self.deleteLater()


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP)
        self.setWindowIcon(app_icon())
        self.resize(1100, 700)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)          # URL drop from a browser (spec §5)
        self.settings = QSettingsCompat()

        self.theme = str(self.settings.value("theme", "system"))
        self.accent = str(self.settings.value("accent", "blue"))
        self._apply_theme()

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self.header = HeaderBar(self)
        self.header.theme_btn.clicked.connect(self._cycle_theme)
        self.header.new_btn.clicked.connect(lambda: self.new_project())
        root_lay.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self._goto)
        self.sidebar.collapse_toggled.connect(
            lambda c: self.settings.setValue("sidebar_collapsed", c))
        body.addWidget(self.sidebar)

        # pages: projects (0), live dashboard (1), settings (2)
        self.stack = QStackedWidget()
        projects_root = str(self.settings.value(
            "projects_root", os.path.join(os.getcwd(), "projects")))
        self.projects_root = projects_root
        self.projects_view = ProjectsView(projects_root)
        self.dashboard = LiveDashboard()
        self.dashboard.preview_requested = self._preview_current
        self.settings_view = SettingsView(
            self.theme, self.accent, projects_root,
            bool(self.settings.value("start_minimized", False)))
        self.stack.addWidget(self.projects_view)
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.settings_view)
        body.addWidget(self.stack, stretch=1)
        root_lay.addLayout(body, stretch=1)

        # status bar (spec §5): ~24px, running info + network dot
        self.status_bar = QFrame()
        self.status_bar.setObjectName("StatusBar")
        self.status_bar.setFixedHeight(24)
        sb_lay = QHBoxLayout(self.status_bar)
        sb_lay.setContentsMargins(14, 2, 14, 2)
        self.status_lbl = QLabel("Ready")
        self.status_lbl.setProperty("muted", True)
        sb_lay.addWidget(self.status_lbl)
        sb_lay.addStretch(1)
        self.health_dot = QLabel("●")
        self.health_dot.setToolTip("Network healthy")
        sb_lay.addWidget(self.health_dot)
        root_lay.addWidget(self.status_bar)

        self.toasts = widgets.ToastHost(self)

        # wiring
        self.projects_view.resume_requested.connect(self.start_project)
        self.projects_view.new_requested.connect(lambda: self.new_project())
        self.projects_view.open_summary_requested.connect(self._open_saved_summary)
        self.settings_view.theme_changed.connect(self.set_theme)
        self.settings_view.accent_changed.connect(self.set_accent)
        self.settings_view.minimized_check.toggled.connect(
            lambda v: self.settings.setValue("start_minimized", v))
        self.settings_view.root_edit.textChanged.connect(self._update_root)

        self._build_tray()
        self._build_palette()
        self._build_shortcuts()

        geo = self.settings.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if bool(self.settings.value("sidebar_collapsed", False)):
            self.sidebar.toggle_collapse(True)

        self._sys_timer = QTimer(self)
        self._sys_timer.timeout.connect(self._poll_system_theme)
        self._sys_timer.start(10_000)

        self._tips = None
        self._shown_once = False

    # ------------------------------------------------------------- theming

    def _apply_theme(self):
        self.tok = theme_engine.apply(QApplication.instance(), self.theme, self.accent)
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("accent", self.accent)
        if hasattr(self, "header"):
            self.header.refresh_theme(self.theme)
        if hasattr(self, "health_dot"):
            self.health_dot.setStyleSheet(
                f"color: {self.tok['success']}; background: transparent;")

    def set_theme(self, theme: str):
        if theme == self.theme and theme != "system":
            return
        self.theme = theme
        self._apply_theme()
        if hasattr(self, "settings_view"):
            self.settings_view._theme = theme
            self.settings_view._sync_visual_state()

    def set_accent(self, accent: str):
        self.accent = accent
        self._apply_theme()
        if hasattr(self, "settings_view"):
            self.settings_view._accent = accent
            self.settings_view._sync_visual_state()

    _set_theme = set_theme               # legacy hook

    def _cycle_theme(self):
        nxt = THEME_CYCLE[(THEME_CYCLE.index(self.theme) + 1) % len(THEME_CYCLE)]
        self.set_theme(nxt)
        self.settings_view._theme = nxt
        self.settings_view._sync_visual_state()
        self.toasts.toast(f"Theme: {nxt}", "info", 1600)

    def _poll_system_theme(self):
        if self.theme == "system":
            now = theme_engine.system_prefers_dark()
            if getattr(self, "_last_system", None) != now:
                self._last_system = now
                self._apply_theme()

    # ------------------------------------------------------------- navigation

    def _goto(self, idx: int):
        if idx == 1:
            self.new_project()
            self.sidebar.set_active(0)
            return
        if idx == 0:
            self.projects_view.projects_root = self.projects_root
            self.projects_view.refresh()
        anim.cross_fade(self.stack, idx)

    def show_projects(self) -> None:
        self.sidebar.set_active(0)
        self.projects_view.refresh()
        anim.cross_fade(self.stack, 0)

    def _goto_settings(self):
        self.sidebar.set_active(2)
        anim.cross_fade(self.stack, 2)

    # ------------------------------------------------------------- projects

    def new_project(self, seed_url: str = ""):
        wizard = NewProjectWizard(self.projects_root, seed_url)
        wizard.started.connect(self.start_project)
        wizard.exec()

    def start_project(self, cfg: CrawlConfig):
        if self.dashboard.worker is not None and self.dashboard.worker.isRunning():
            QMessageBox.information(self, APP,
                                    "A project is already running — cancel it first.")
            return
        if cfg.headless and not self._headless_ready():
            return
        os.makedirs(cfg.output_dir, exist_ok=True)
        self.sidebar.set_active(0)
        anim.cross_fade(self.stack, 1)
        self.dashboard.start_project(cfg, lambda c: ProjectWorker(c))
        self._watch(self.dashboard.worker)

    def _watch(self, worker: ProjectWorker):
        worker.progress_updated.connect(self._update_status)
        worker.run_finished.connect(self._on_run_finished)

    def _update_status(self, payload: dict):
        running = 1 if self.dashboard.worker and self.dashboard.worker.isRunning() else 0
        speed = payload.get("mbps_recent", 0)
        txt = f"{running} project{'s' if running != 1 else ''} running"
        if speed:
            txt += f" · {speed:.1f} MB/s"
        if payload.get("paused"):
            txt += " · paused"
        self.status_lbl.setText(txt)
        self._tray_badge(1 if running else 0)

    def _on_run_finished(self, report):
        self._tray_badge(0)
        fails = len(report.failures)
        if self.isMinimized() and self.tray is not None:
            self.tray.showMessage(
                APP,
                f"Crawl completed: {report.stats.pages_saved} pages, "
                f"{report.stats.assets_saved} assets"
                + (f" ({fails} issues)" if fails else ""),
                QSystemTrayIcon.Information, 4000)
        self.toasts.toast(
            f"Crawl completed — {report.stats.pages_saved} pages, "
            f"{report.stats.assets_saved} assets",
            "success" if not fails else "warning")
        self.status_lbl.setText("Idle")

    def _open_saved_summary(self, summary: ProjectSummary):
        self.dashboard.show_project_summary(summary)
        anim.cross_fade(self.stack, 1)

    def _update_root(self, text: str):
        text = text.strip()
        if text and text != self.projects_root:
            self.projects_root = text
            self.settings.setValue("projects_root", text)
            self.projects_view.projects_root = text
            self.projects_view.refresh()

    # ------------------------------------------------------------- preview

    def _preview_current(self):
        folder = self.dashboard.current_output or self.projects_view_selected_folder()
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, APP, "Pick a project first — nothing to preview.")
            return
        if getattr(self, "_preview", None) is None:
            from app.gui.preview import PreviewController

            self._preview = PreviewController(self)
            self._preview.failed.connect(
                lambda msg: self.toasts.toast(f"Preview failed: {msg}", "error"))
        self._preview.open_mirror(folder)

    def projects_view_selected_folder(self) -> str:
        s = self.projects_view._visible_first()
        return s.folder if s else ""

    @staticmethod
    def _headless_ready() -> bool:
        """Playwright browsers are deliberately NOT bundled (phase 8): the
        one-time `playwright install chromium` is a user step."""
        try:
            import glob
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
            if glob.glob(os.path.join(root, "chromium*")):
                return True
        except Exception:                      # noqa: BLE001 — prompt on any doubt
            pass
        QMessageBox.warning(
            None, APP,
            "Headless crawling needs a one-time browser download.\n\n"
            "Run this in a terminal once:\n"
            "    playwright install chromium\n\n"
            "Then start the project again.")
        return False

    # ------------------------------------------------------------- tray (spec §5)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        show_action = menu.addAction("Show")
        show_action.triggered.connect(self._restore_from_tray)
        menu.addAction("Pause current project").triggered.connect(self.pause)
        menu.addAction("Resume current project").triggered.connect(self.resume)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._restore_from_tray()
            if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _restore_from_tray(self):
        self.showNormal()
        self.activateWindow()

    def _tray_badge(self, count: int):
        from PySide6.QtCore import QRectF as _RF

        if count <= 0:
            self.tray.setIcon(app_icon())
            self.tray.setToolTip(APP)
            return
        pm = app_icon().pixmap(64, 64)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor("#E81123")))
        p.setPen(Qt.NoPen)
        p.drawEllipse(38, 4, 24, 24)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(_RF(38, 4, 24, 24), Qt.AlignCenter, str(count))
        p.end()
        self.tray.setIcon(QIcon(pm))
        self.tray.setToolTip(f"{APP} — {count} download active")

    # ------------------------------------------------------------- palette + shortcuts

    def _build_palette(self):
        self.palette_dlg = CommandPalette(self)
        self.palette_dlg.triggered.connect(self._palette_action)

    def _palette_entries(self):
        entries = [
            ("Go to Projects", "view the saved projects", ("nav", 0)),
            ("New Project", "start a new mirror", ("action", "new")),
            ("Open Settings", "theme, accent, defaults", ("nav", 2)),
            ("Toggle theme", "light / dark cycle", ("action", "theme")),
            ("Pause / Resume", "the running project", ("action", "toggle")),
            ("Preview in browser", "serve the current mirror", ("action", "preview")),
        ]
        for s in getattr(self.projects_view, "summaries", []):
            entries.append((f"Project: {s.name}", s.seed, ("project", s)))
        return entries

    def _palette_action(self, payload):
        kind, value = payload
        if kind == "nav":
            if value == 0:
                self.show_projects()
            else:
                self._goto_settings()
        elif kind == "action":
            if value == "new":
                self.new_project()
            elif value == "theme":
                self._cycle_theme()
            elif value == "toggle":
                self.toggle_pause()
            elif value == "preview":
                self._preview_current()
        elif kind == "project":
            self._open_saved_summary(value)

    def _build_shortcuts(self):
        from PySide6.QtGui import QAction, QKeySequence

        for name, key, handler in (
                ("palette", "Ctrl+K", self._open_palette),
                ("new", "Ctrl+N", lambda: self.new_project()),
                ("refresh", "Ctrl+R", self._refresh_projects),
                ("settings", "Ctrl+,", self._goto_settings),
                ("toggle", "Space", self.toggle_pause)):
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(handler)
            self.addAction(a)

    def _open_palette(self):
        self.palette_dlg.open_with(self._palette_entries())

    def _refresh_projects(self):
        self.projects_view.refresh()
        self.toasts.toast("Projects refreshed", "info", 2000)

    # ------------------------------------------------------------- controls

    def pause(self):
        if self.dashboard.worker is not None:
            self.dashboard.pause()

    def resume(self):
        if self.dashboard.worker is not None:
            self.dashboard.resume()

    def toggle_pause(self):
        w = self.dashboard.worker
        focus = QApplication.focusWidget()
        if w is None or not w.isRunning():
            return
        if isinstance(focus, (QLineEdit, QListWidget)):
            return                        # don't steal Space from text entry
        if self.dashboard.labels["state"].text() == "Paused":
            self.dashboard.resume()
        else:
            self.dashboard.pause()

    # ------------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event):              # noqa: N802 — Qt naming
        md = event.mimeData()
        if md.hasText() or md.hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):                   # noqa: N802 — Qt naming
        md = event.mimeData()
        text = (md.text() if md.hasText()
                else (md.urls()[0].toString() if md.urls() else ""))
        if text:
            self.new_project(seed_url=text.strip())
            event.acceptProposedAction()

    # ------------------------------------------------------------- tips + show

    def showEvent(self, event):                    # noqa: N802 — Qt naming
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            self.projects_view.refresh()
            if not bool(self.settings.value("onboarded", False)):
                QTimer.singleShot(350, self._show_tips)

    def _show_tips(self):
        if not self.isVisible():
            return
        self._tips = TipCard(self)
        self._tips.move(self.width() - self._tips.width() - 16,
                        self.height() - self._tips.height() - 40)
        self._tips.show()
        self._tips.raise_()
        self._tips.dismissed.connect(
            lambda: self.settings.setValue("onboarded", True))
        anim.fade_in(self._tips)

    def resizeEvent(self, event):                  # noqa: N802 — Qt naming
        super().resizeEvent(event)
        if getattr(self, "_tips", None) is not None and self._tips.isVisible():
            self._tips.move(self.width() - self._tips.width() - 16,
                            self.height() - self._tips.height() - 40)

    # ------------------------------------------------------------- close/save

    def closeEvent(self, event):                   # noqa: N802 — Qt naming
        worker = self.dashboard.worker
        if worker is not None and worker.isRunning():
            ret = QMessageBox.question(
                self, APP, "A crawl is running. Cancel it and exit?")
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            worker.cancel()
            if not worker.wait(5000):
                worker.terminate()
        self.settings.setValue("window_geometry", self.saveGeometry())
        self.settings.setValue("sidebar_collapsed", self.sidebar.is_collapsed())
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("accent", self.accent)
        super().closeEvent(event)

    # ------------------------------------------------------------- legacy test hooks

    @property
    def worker(self):
        return self.dashboard.worker

    @property
    def pages_list(self):
        return self.dashboard.pages_list

    @property
    def labels(self):
        return self.dashboard.labels

    @property
    def progress(self):
        return self.dashboard.progress

    def cancel(self):
        self.dashboard.cancel()


def QSettingsCompat():
    from PySide6.QtCore import QSettings
    return QSettings(ORG, APP)
