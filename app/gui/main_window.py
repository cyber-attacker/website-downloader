"""Main window shell (spec §4/§5): frameless window with a custom title bar
(Windows-native hit-testing keeps drag, resize and Aero snap), persistent
sidebar, cross-fade page transitions, toast host, command palette (Ctrl+K),
global shortcuts, system tray with an active-download badge, drag-and-drop
URL intake, status bar, first-run onboarding, and saved window state."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from PySide6.QtCore import QElapsedTimer, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontDatabase, QIcon, QPainter, QPen, QPixmap
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
from app.gui.sidebar import COLLAPSED_W, EXPANDED_W, Sidebar
from app.gui.worker import ProjectWorker

ORG = "WebsiteDownloader"
APP = "Website Downloader"

# Windows nonclient hit-test codes
HTCLIENT = 1
HTCAPTION = 2
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
WM_NCHITTEST = 0x84
WM_NCLBUTTONDBLCLK = 0xA3
WM_GETMINMAXINFO = 0x24
RESIZE_MARGIN = 6


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
    # arrow shaft + head + tray base line
    p.drawRect(QRectF(29, 16, 6, 20))
    from PySide6.QtGui import QPolygon
    p.drawPolygon(QPolygon([QPoint(22, 32), QPoint(42, 32), QPoint(32, 46)]))
    p.drawRect(QRectF(20, 50, 24, 5))
    p.end()
    return QIcon(pm)


class TitleBar(QFrame):
    """Custom title strip: brand + minimize / maximize / close (close turns
    red on hover, spec §4). Dragging is handled by the OS via HTCAPTION."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("TitleBar")
        self.setFixedHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 6, 0)
        lay.setSpacing(8)
        self.icon_lbl = QLabel()
        self.icon_lbl.setPixmap(app_icon().pixmap(18, 18))
        lay.addWidget(self.icon_lbl)
        self.title = QLabel("Website Downloader")
        lay.addWidget(self.title)
        lay.addStretch(1)

        self.min_btn = self._btn("minus", "Minimize")
        self.min_btn.clicked.connect(self.window().showMinimized)
        self.max_btn = self._btn("square", "Maximize")
        self.max_btn.clicked.connect(self._toggle_max)
        self.close_btn = self._btn("times", "Close")
        self.close_btn.clicked.connect(self.window().close)
        for b in (self.min_btn, self.max_btn, self.close_btn):
            lay.addWidget(b)
        self._restyle()

    def _btn(self, glyph: str, tip: str) -> QPushButton:
        b = QPushButton()
        b.setObjectName("TitleBtn")
        b.setFixedSize(34, 28)
        b.setToolTip(tip)
        self._glyphs = getattr(self, "_glyphs", {})
        self._glyphs[b] = glyph
        return b

    def _toggle_max(self):
        w = self.window()
        w.showNormal() if w.isMaximized() else w.showMaximized()

    def _restyle(self):
        tok = theme_engine.tokens()
        for b, glyph in getattr(self, "_glyphs", {}).items():
            b.setIcon(widgets.icon(glyph, tok["text2"]))
            is_close = b is self.close_btn
            b.setStyleSheet(f"""
                QPushButton#TitleBtn {{ background: transparent; border: none;
                    border-radius: 4px; }}
                QPushButton#TitleBtn:hover {{
                    background: {'#E81123' if is_close else tok['hover']};
                }}
            """)

    def refresh_theme(self):
        self.icon_lbl.setPixmap(app_icon().pixmap(18, 18))
        self._restyle()


class CommandPalette(QDialog):
    """Ctrl+K overlay (spec §5): type to jump to projects, screens, actions."""

    triggered = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
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


class Onboarding(QWidget):
    """First-run welcome overlay: 2-3 pointers, dismissed once (spec §5)."""

    done = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        card = widgets.Card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(28, 24, 28, 24)
        cl.setSpacing(10)
        t = QLabel("Welcome to Website Downloader")
        t.setProperty("heading", True)
        cl.addWidget(t)
        for line in (
            "1.  Start with New Project in the sidebar — paste a URL (or drag "
            "one straight from your browser).",
            "2.  Pause, resume or cancel any crawl live; killed crawls resume "
            "byte-identically later.",
            "3.  Press Ctrl+K anytime for the command palette; pick a theme "
            "in Settings."):
            lab = QLabel(line)
            lab.setWordWrap(True)
            cl.addWidget(lab)
        btn = QPushButton("Got it")
        btn.setProperty("variant", "primary")
        btn.clicked.connect(self._dismiss)
        cl.addSpacing(8)
        cl.addWidget(btn, 0, Qt.AlignRight)
        lay.addWidget(card)
        self.setStyleSheet("background: rgba(0,0,0,0.45);")

    def _dismiss(self):
        self.done.emit()
        self.hide()
        self.deleteLater()


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setMinimumSize(880, 560)
        self.settings = QSettingsCompat()
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)          # URL drop from a browser (spec §5)

        self.theme = str(self.settings.value("theme", "system"))
        self.accent = str(self.settings.value("accent", "blue"))
        self._apply_theme()

        root_lay = QVBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self.titlebar = TitleBar(self)
        root_lay.addWidget(self.titlebar)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self._goto)
        self.sidebar.collapse_toggled.connect(
            lambda collapsed: self.settings.setValue("sidebar_collapsed", collapsed))
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
        self.projects_view.new_requested.connect(self.new_project)
        self.projects_view.open_summary_requested.connect(self._open_saved_summary)
        self.settings_view.theme_changed.connect(self.set_theme)
        self.settings_view.accent_changed.connect(self.set_accent)
        self.settings_view.minimized_check.toggled.connect(
            lambda v: self.settings.setValue("start_minimized", v))
        self.settings_view.root_edit.textChanged.connect(
            lambda text: self._update_root(text))

        self._build_tray()
        self._build_palette()
        self._build_shortcuts()

        # restore window state (spec §5)
        geo = self.settings.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        if bool(self.settings.value("sidebar_collapsed", False)):
            self.sidebar.toggle_collapse(True)

        # system theme re-poll (spec §2E) + tray-badge refresh
        self._sys_timer = QTimer(self)
        self._sys_timer.timeout.connect(self._poll_system_theme)
        self._sys_timer.start(10_000)

        # first-run onboarding
        self._onboarding = None
        self._shown_once = False

    # ------------------------------------------------------------- theming

    def _apply_theme(self):
        self.tok = theme_engine.apply(QApplication.instance(), self.theme, self.accent)
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("accent", self.accent)
        if hasattr(self, "titlebar"):
            self.titlebar.refresh_theme()
        self._health_dot_color()

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

    _set_theme = set_theme               # legacy hook used by older callers

    def _poll_system_theme(self):
        if self.theme == "system":
            # resolve() flips when the OS switches; re-apply when it does
            if getattr(self, "_last_system", None) is None:
                self._last_system = theme_engine.system_prefers_dark()
            elif self._last_system != theme_engine.system_prefers_dark():
                self._last_system = theme_engine.system_prefers_dark()
                self._apply_theme()

    def _health_dot_color(self):
        if not hasattr(self, "health_dot"):
            return
        tok = self.tok
        color = tok["success"]
        self.health_dot.setStyleSheet(f"color: {color}; background: transparent;")
        self.health_dot.setToolTip("Network healthy")

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
        paused = payload.get("paused")
        if paused:
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
        one-time `playwright install chromium` is a user step (spec §11)."""
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
        self.tray_pause = menu.addAction("Pause current project")
        self.tray_pause.triggered.connect(self.pause)
        self.tray_resume = menu.addAction("Resume current project")
        self.tray_resume.triggered.connect(self.resume)
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
        if count <= 0:
            self.tray.setIcon(app_icon())
            self.tray.setToolTip(APP)
            return
        tok = self.tok
        pm = app_icon().pixmap(64, 64)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QBrush(QColor("#E81123")))
        p.setPen(QPen(QColor("#FFFFFF"), 3))
        p.drawEllipse(38, 6, 22, 22)
        f = QFont(); f.setBold(True); f.setPixelSize(14)
        p.setFont(f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRectF(38, 6, 22, 22), Qt.AlignCenter, str(count))
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
            self._goto(value)
        elif kind == "action":
            if value == "new":
                self.new_project()
            elif value == "theme":
                order = ["system", "light", "dark", "midnight", "high_contrast"]
                self.set_theme(order[(order.index(self.theme) + 1) % len(order)])
                self.settings_view._theme = self.theme
                self.settings_view._sync_visual_state()
            elif value == "toggle":
                self.toggle_pause()
            elif value == "preview":
                self._preview_current()
        elif kind == "project":
            self._open_saved_summary(value)

    def _build_shortcuts(self):
        from PySide6.QtGui import QKeySequence, QAction
        acts = {
            "palette": self._open_palette,
            "new": lambda: self.new_project(),
            "refresh": self._refresh_projects,
            "settings": self._goto_settings,
            "toggle": self.toggle_pause,
        }
        keys = {
            "palette": "Ctrl+K",
            "new": "Ctrl+N",
            "refresh": "Ctrl+R",
            "settings": "Ctrl+,",
            "toggle": "Space",
        }
        for name, handler in acts.items():
            a = QAction(self)
            a.setShortcut(QKeySequence(keys[name]))
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

    # ------------------------------------------------------------- onboarding + show

    def showEvent(self, event):                    # noqa: N802 — Qt naming
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            self.projects_view.refresh()
            if not bool(self.settings.value("onboarded", False)):
                QTimer.singleShot(250, self._show_onboarding)

    def _show_onboarding(self):
        self._onboarding = Onboarding(self)
        self._onboarding.setGeometry(self.rect())
        self._onboarding.done.connect(
            lambda: self.settings.setValue("onboarded", True))
        self._onboarding.show()
        anim.fade_in(self._onboarding)

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

    # ------------------------------------------------------------- frameless plumbing

    def _hit_test(self, pos: QPoint) -> int:
        """OS-level hit zones: resize edges, caption drag, client content."""
        w, h = self.width(), self.height()
        m = RESIZE_MARGIN
        x, y = pos.x(), pos.y()
        title = self.titlebar.geometry()
        over_button = False
        for b in (self.titlebar.min_btn, self.titlebar.max_btn, self.titlebar.close_btn):
            if b.geometry().contains(self.titlebar.mapFrom(self, pos)):
                over_button = True
        if x < m and y < m:
            return HTTOPLEFT
        if x >= w - m and y < m:
            return HTTOPRIGHT
        if x < m and y >= h - m:
            return HTBOTTOMLEFT
        if x >= w - m and y >= h - m:
            return HTBOTTOMRIGHT
        if x < m:
            return HTLEFT
        if x >= w - m:
            return HTRIGHT
        if y < m:
            return HTTOP
        if y >= h - m:
            return HTBOTTOM
        if title.contains(pos) and not over_button:
            return HTCAPTION
        return HTCLIENT

    def nativeEvent(self, eventType, message):     # noqa: N802 — Qt naming
        if eventType == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_NCHITTEST:
                pos = self.mapFromGlobal(QPoint(msg.pt.x, msg.pt.y))
                return True, self._hit_test(pos)
            if msg.message == WM_NCLBUTTONDBLCLK:
                self.showNormal() if self.isMaximized() else self.showMaximized()
                return True, 0
            if msg.message == WM_GETMINMAXINFO and self.isMaximized():
                # keep the maximized frameless window inside the work area
                mmi = wintypes.MINMAXINFO.from_address(msg.lParam)
                hwnd = int(self.winId())
                monitor = ctypes.windll.user32.MonitorFromWindow(
                    hwnd, 2)              # MONITOR_DEFAULTTONEAREST
                info = wintypes.MONITORINFOEX()
                info.cbSize = ctypes.sizeof(wintypes.MONITORINFOEX)
                if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    wa = info.rcWork
                    mmi.ptMaxPosition.x = wa.left
                    mmi.ptMaxPosition.y = wa.top
                    mmi.ptMaxSize.x = wa.right - wa.left
                    mmi.ptMaxSize.y = wa.bottom - wa.top
                    return True, 0
        return super().nativeEvent(eventType, message)

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
