"""Main window shell (phase 5): Projects dashboard + crawl dashboard pages,
dark/light/system theming, settings persisted via QSettings."""
from __future__ import annotations

import os

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
)

from app.core.projects import scan_projects
from app.core.types import CrawlConfig
from app.gui.crawl_dashboard import CrawlDashboard, NewProjectDialog
from app.gui.projects_panel import ProjectsPanel
from app.gui.theme import apply_theme

ORG = "WebsiteDownloader"
APP = "Website Downloader"


class SettingsDialog(QDialog):
    def __init__(self, parent=None, projects_root: str = "", theme: str = "system") -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        form = QFormLayout(self)

        self.root_edit = QLineEdit(projects_root, self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        form.addRow("Projects folder", self.root_edit)

        self.theme_combo = QComboBox(self)
        self.theme_combo.addItems(["system", "dark", "light"])
        self.theme_combo.setCurrentText(theme)
        form.addRow("Theme", self.theme_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose the projects folder")
        if d:
            self.root_edit.setText(d)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(900, 620)
        self.settings = QSettings(ORG, APP)

        projects_root = str(self.settings.value("projects_root", ""))
        if not projects_root:
            projects_root = os.path.join(os.getcwd(), "projects")
            self.settings.setValue("projects_root", projects_root)
        self.projects_root = projects_root

        self.stack = QStackedWidget()
        self.projects_panel = ProjectsPanel(self.projects_root)
        self.dashboard = CrawlDashboard()
        self.stack.addWidget(self.projects_panel)     # page 0
        self.stack.addWidget(self.dashboard)         # page 1
        self.setCentralWidget(self.stack)
        self.preview = None

        self._build_menus()

        # panel wiring
        self.projects_panel.new_requested.connect(self.new_project)
        self.projects_panel.resume_requested.connect(self.start_project)
        self.projects_panel.open_summary_requested.connect(self._open_saved_summary)

        self._apply_saved_theme()

    # ---- menus ---------------------------------------------------------------

    def _build_menus(self) -> None:
        project_menu = self.menuBar().addMenu("&Project")

        new_action = QAction("&New Project…", self)
        new_action.triggered.connect(self.new_project)
        project_menu.addAction(new_action)

        refresh_action = QAction("&Refresh projects", self)
        refresh_action.triggered.connect(self.show_projects)
        project_menu.addAction(refresh_action)

        folder_action = QAction("Open projects &folder", self)
        folder_action.triggered.connect(self._open_projects_root)
        project_menu.addAction(folder_action)

        settings_action = QAction("&Settings…", self)
        settings_action.triggered.connect(self._edit_settings)
        project_menu.addAction(settings_action)
        project_menu.addSeparator()

        preview_action = QAction("&Preview in browser", self)
        preview_action.triggered.connect(self._preview_current)
        project_menu.addAction(preview_action)
        project_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.triggered.connect(self.close)
        project_menu.addAction(quit_action)

        theme_menu = self.menuBar().addMenu("&Theme")
        group = QActionGroup(self)
        self._theme_actions: dict[str, QAction] = {}
        current = str(self.settings.value("theme", "system"))
        for name in ("system", "dark", "light"):
            act = QAction(name.title(), self, checkable=True)
            act.setChecked(name == current)
            act.triggered.connect(lambda checked=False, n=name: self._set_theme(n))
            group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions[name] = act

    def _set_theme(self, name: str) -> None:
        self.settings.setValue("theme", name)
        apply_theme(self.app(), name)
        act = self._theme_actions.get(name)
        if act is not None:
            act.setChecked(True)

    def _apply_saved_theme(self) -> None:
        apply_theme(self.app(), str(self.settings.value("theme", "system")))

    def app(self):
        from PySide6.QtWidgets import QApplication
        return QApplication.instance()

    # ---- navigation ------------------------------------------------------------

    def show_projects(self) -> None:
        self.projects_panel.projects_root = self.projects_root
        self.projects_panel.refresh()
        self.stack.setCurrentIndex(0)

    def _open_saved_summary(self, summary) -> None:
        self.dashboard.show_project_summary(summary)
        self.stack.setCurrentIndex(1)

    def _open_projects_root(self) -> None:
        os.makedirs(self.projects_root, exist_ok=True)
        os.startfile(self.projects_root)  # noqa: S606 — explorer; user-initiated

    # ---- preview in browser (phase 6) --------------------------------------------

    def _preview_current(self) -> None:
        folder = self.dashboard.current_output
        if not folder or not os.path.isdir(folder):
            selected = self.projects_panel.selected()
            folder = selected.folder if selected is not None else ""
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, APP,
                                    "Pick a project first — nothing to preview.")
            return
        if self.preview is None:
            from app.gui.preview import PreviewController

            self.preview = PreviewController(self)
            self.preview.failed.connect(
                lambda msg: QMessageBox.warning(self, APP, f"Preview failed: {msg}"))
        self.preview.open_mirror(folder)

    # ---- settings ----------------------------------------------------------------

    def _edit_settings(self) -> None:
        dlg = SettingsDialog(self, self.projects_root,
                              str(self.settings.value("theme", "system")))
        if dlg.exec() != QDialog.Accepted:
            return
        new_root = dlg.root_edit.text().strip()
        if new_root and new_root != self.projects_root:
            self.projects_root = new_root
            self.settings.setValue("projects_root", new_root)
            self.projects_panel.projects_root = new_root
            self.projects_panel.refresh()
        self._set_theme(dlg.theme_combo.currentText())
        self._theme_actions[dlg.theme_combo.currentText()].setChecked(True)

    # ---- projects ------------------------------------------------------------------

    def new_project(self) -> None:
        default_out = os.path.join(self.projects_root, "new-project")
        dlg = NewProjectDialog(self, default_output=default_out)
        if dlg.exec() != QDialog.Accepted:
            return
        cfg = dlg.config()
        if cfg is None:
            return
        os.makedirs(cfg.output_dir, exist_ok=True)
        self.start_project(cfg)

    def start_project(self, cfg: CrawlConfig) -> None:
        if self.dashboard.worker is not None and self.dashboard.worker.isRunning():
            QMessageBox.information(self, APP,
                                    "A project is already running — cancel it first.")
            return
        if cfg.headless and not self._headless_ready():
            return
        self.stack.setCurrentIndex(1)
        self.dashboard.start_project(cfg)

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

    # ---- test/legacy convenience -----------------------------------------------

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

    def pause(self) -> None:
        self.dashboard.pause()

    def resume(self) -> None:
        self.dashboard.resume()

    def cancel(self) -> None:
        self.dashboard.cancel()

    # ---- shutdown -------------------------------------------------------------

    def closeEvent(self, event) -> None:          # noqa: N802 — Qt naming
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
        event.accept()
