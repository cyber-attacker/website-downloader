"""Projects dashboard panel (phase 5): every saved project under the projects
root, its status/progress from project_meta + the urls table, Resume, and a
double-click 'open dashboard view' for saved rows."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.projects import ProjectSummary, config_for, scan_projects
from app.core.types import CrawlConfig

COLUMNS = ["Project", "Seed", "Status", "Progress", "Pages", "Last run"]


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class ProjectsPanel(QWidget):
    resume_requested = Signal(object)      # CrawlConfig for the chosen project
    new_requested = Signal()
    open_summary_requested = Signal(object)  # ProjectSummary (double-click)

    def __init__(self, projects_root: str, parent=None) -> None:
        super().__init__(parent)
        self.projects_root = projects_root
        layout = QVBoxLayout(self)

        buttons = QHBoxLayout()
        new_btn = QPushButton("New Project…")
        new_btn.clicked.connect(self.new_requested.emit)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        self.resume_btn = QPushButton("Resume", enabled=False)
        self.resume_btn.clicked.connect(self._resume_selected)
        self.open_btn = QPushButton("Open Folder", enabled=False)
        self.open_btn.clicked.connect(self._open_selected_folder)
        for b in (new_btn, refresh_btn, self.resume_btn, self.open_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table, stretch=1)

        self.summaries: list[ProjectSummary] = []
        self.refresh()

    # ---- data ----------------------------------------------------------------

    def refresh(self) -> None:
        self.summaries = scan_projects(self.projects_root)
        self.table.setRowCount(len(self.summaries))
        for r, s in enumerate(self.summaries):
            items = [
                s.name,
                s.seed,
                s.status_display,
                f"{s.progress_pct}%",
                str(s.pages_done),
                _fmt_ts(s.last_run_at),
            ]
            for c, text in enumerate(items):
                item = QTableWidgetItem(text)
                if c == 2 and s.status == "incomplete":
                    item.setData(0, f"{s.status_display} ({s.progress_pct}%)")
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self._selection_changed()

    def selected(self) -> ProjectSummary | None:
        row = self.table.currentRow()
        if 0 <= row < len(self.summaries):
            return self.summaries[row]
        return None

    # ---- actions ---------------------------------------------------------------

    def _selection_changed(self) -> None:
        s = self.selected()
        self.resume_btn.setEnabled(s is not None and s.status == "incomplete")
        self.open_btn.setEnabled(s is not None)

    def _resume_selected(self) -> None:
        s = self.selected()
        if s is None:
            return
        self.resume_requested.emit(config_for(s))

    def _double_clicked(self, *_):
        s = self.selected()
        if s is not None:
            self.open_summary_requested.emit(s)

    def _open_selected_folder(self) -> None:
        s = self.selected()
        if s is not None:
            os.startfile(s.folder)  # noqa: S606 — explorer, user-initiated
