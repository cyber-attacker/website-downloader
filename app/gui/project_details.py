"""Project details view: the deep screen for one saved project. Shows the
crawl's real page tree (from the urls + link_edges graph in project.db),
stats, and the per-project actions: Resume, Open folder, Preview in browser,
Export as .zip. Reached by double-clicking a project card."""
from __future__ import annotations

import os
import zipfile

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.projects import ProjectSummary, config_for, load_page_tree
from app.gui import anim, theme_engine, widgets
from app.gui.widgets import Card, StatCard, StatusPill


class ProjectDetails(QWidget):
    back_requested = Signal()
    resume_requested = Signal(object)        # CrawlConfig
    preview_requested = Signal(str)          # project folder
    export_requested = Signal(object)        # ProjectSummary

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 12)
        lay.setSpacing(14)

        # ---- top bar
        top = QHBoxLayout()
        back = widgets.icon_btn("arrow-left", "Back to Projects")
        back.clicked.connect(self.back_requested.emit)
        top.addWidget(back)
        self.name_lbl = QLabel("Project")
        self.name_lbl.setProperty("heading", True)
        top.addWidget(self.name_lbl)
        self.pill = widgets.StatusPill("", "muted")
        top.addWidget(self.pill)
        top.addStretch(1)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setProperty("variant", "primary")
        self.resume_btn.clicked.connect(self._emit_resume)
        self.open_btn = widgets.icon_btn("folder", "Open project folder")
        self.open_btn.clicked.connect(self._open_folder)
        self.preview_btn = widgets.icon_btn("globe", "Preview in browser")
        self.preview_btn.clicked.connect(lambda: self.preview_requested.emit(self._folder()))
        self.export_btn = widgets.icon_btn("file-archive", "Export as .zip")
        self.export_btn.clicked.connect(self._export_zip)
        top.addWidget(self.resume_btn)
        top.addWidget(self.open_btn)
        top.addWidget(self.preview_btn)
        top.addWidget(self.export_btn)
        lay.addLayout(top)

        # ---- hero: seed + stats
        hero = Card()
        hero_lay = QVBoxLayout(hero)
        hero_lay.setContentsMargins(18, 14, 18, 14)
        hero_lay.setSpacing(10)
        self.seed_lbl = QLabel("")
        self.seed_lbl.setProperty("muted", True)
        hero_lay.addWidget(self.seed_lbl)
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_pages = StatCard("Pages saved", "file-alt")
        self.stat_assets = StatCard("Assets saved", "image")
        self.stat_status = StatCard("Total URLs", "project-diagram")
        self.stat_run = StatCard("Progress", "check-double")
        for card in (self.stat_pages, self.stat_assets, self.stat_status,
                     self.stat_run):
            stats.addWidget(card, stretch=1)
        hero_lay.addLayout(stats)
        self.progress = widgets.AnimatedProgressBar("completed")
        self.progress.setFixedHeight(6)
        hero_lay.addWidget(self.progress)
        lay.addWidget(hero)

        # ---- page tree
        tree_card = Card()
        tree_lay = QVBoxLayout(tree_card)
        tree_lay.setContentsMargins(10, 8, 10, 10)
        tree_head = QHBoxLayout()
        tree_title = QLabel("Crawled pages")
        tree_title.setProperty("cardtitle", True)
        tree_head.addWidget(tree_title)
        tree_head.addStretch(1)
        self.tree_count = QLabel("0")
        self.tree_count.setProperty("muted", True)
        tree_head.addWidget(self.tree_count)
        tree_lay.addLayout(tree_head)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._open_tree_file)
        tree_lay.addWidget(self.tree)
        lay.addWidget(tree_card, stretch=1)

        self.summary: ProjectSummary | None = None

    # ------------------------------------------------------------- populate

    def set_summary(self, summary: ProjectSummary) -> None:
        self.summary = summary
        self.name_lbl.setText(summary.name)
        self.seed_lbl.setText(summary.seed)
        pill_state, pill_text = {
            "complete": ("completed", "Completed"),
            "incomplete": ("running", "Resumable"),
            "empty": ("muted", "No data"),
        }[summary.status]
        self.pill.set_state(pill_state, pill_text)
        self.resume_btn.setVisible(summary.status == "incomplete")

        self.stat_pages.set_value(summary.pages_done)
        self.stat_assets.set_value(summary.assets_done)
        self.stat_status.set_value(summary.urls_total)
        self.stat_run.set_value(summary.progress_pct, fmt="{}%")
        self.progress.set_value_now(summary.progress_pct)
        self.progress.setProperty("status",
                                  "completed" if summary.status == "complete" else "")

        self.tree.clear()
        self._tree_nodes: dict[str, QTreeWidgetItem] = {}
        self._paths: dict[str, str] = {}          # url -> saved file path
        count = 0

        def add(nodes, parent_item):
            nonlocal count
            for node in nodes:
                item = QTreeWidgetItem([node["label"]])
                item.setToolTip(0, node["url"])
                if node["status"] != "done":
                    item.setForeground(0, QColor(theme_engine.tokens()["warning"]))
                if parent_item is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent_item.addChild(item)
                if node["path"]:
                    self._paths[node["url"]] = os.path.join(
                        summary.folder, *node["path"].split("/"))
                count += 1
                add(node["children"], item)

        add(load_page_tree(summary.folder), None)
        self.tree.expandToDepth(0)
        self.tree_count.setText(f"{count:,} URLs")
        anim.fade_in(self)

    # ------------------------------------------------------------- actions

    def _folder(self) -> str:
        return self.summary.folder if self.summary else ""

    def _emit_resume(self):
        if self.summary is not None:
            self.resume_requested.emit(config_for(self.summary))

    def _open_folder(self):
        folder = self._folder()
        if folder:
            os.startfile(folder)              # noqa: S606 — user-initiated

    def _open_tree_file(self, item: QTreeWidgetItem, _col: int):
        """Double-click a page in the tree -> open the saved local file."""
        url = item.toolTip(0)
        path = self._paths.get(url)
        if path and os.path.isfile(path):
            os.startfile(path)                # noqa: S606 — user-initiated

    def _export_zip(self):
        if self.summary is None:
            return
        folder = self.summary.folder
        default = os.path.join(os.path.expanduser("~"), "Downloads",
                               f"{self.summary.name}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Export project", default,
                                              "Zip archive (*.zip)")
        if not path:
            return
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, _dirs, files in os.walk(folder):
                    for name in files:
                        full = os.path.join(dirpath, name)
                        zf.write(full, os.path.relpath(full, folder))
            QMessageBox.information(self, "Website Downloader", "Export complete.")
        except OSError as exc:
            QMessageBox.warning(self, "Website Downloader", f"Export failed: {exc}")
