"""Screen 1 — Projects dashboard (spec §6): card rows (not a table grid),
inline search, status filter chips, sort control, right-click context menu,
and designed empty/loading/error states."""
from __future__ import annotations

import os
import shutil
import time
import zipfile

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.core.projects import ProjectSummary, config_for, scan_projects
from app.core.types import CrawlConfig
from app.gui import anim, theme_engine, widgets
from app.gui.widgets import Card, EmptyState, SkeletonRows, StatusPill

CHIP_ALL, CHIP_RUNNING, CHIP_DONE, CHIP_FAILED = "all", "running", "completed", "failed"


def relative_time(ts: float | None) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    for step, label in ((3600 * 24, "day"), (3600, "hour"), (60, "minute")):
        if delta >= step:
            n = int(delta // step)
            return f"{n} {label}{'s' if n != 1 else ''} ago"
    if delta >= 30:
        return f"{int(delta // 60)} minutes ago"
    return "just now"


class ProjectCard(Card):
    """One saved project: name, seed, status pill, animated progress bar,
    counts, last run, hover actions. Right-click for the context menu."""

    open_requested = Signal(object)          # ProjectSummary (double-click)
    resume_requested = Signal(object)       # ProjectSummary
    duplicate_requested = Signal(object)    # ProjectSummary
    changed = Signal()                      # list mutated (rename/delete/export)

    def __init__(self, summary: ProjectSummary, parent=None):
        super().__init__(parent)
        self.summary = summary
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 14, 14)
        lay.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(3)
        self.name_lbl = QLabel(summary.name)
        self.name_lbl.setProperty("cardtitle", True)
        left.addWidget(self.name_lbl)
        seed = QLabel(summary.seed)
        seed.setProperty("muted", True)
        left.addWidget(seed)
        meta = QHBoxLayout()
        meta.setSpacing(14)
        self.pages_lbl = QLabel(f"{summary.pages_done:,} pages")
        self.pages_lbl.setProperty("muted", True)
        self.run_lbl = QLabel(relative_time(summary.last_run_at))
        self.run_lbl.setProperty("muted", True)
        meta.addWidget(self.pages_lbl)
        meta.addWidget(self.run_lbl)
        left.addLayout(meta)
        lay.addLayout(left, stretch=1)

        pill_state = {"complete": "completed", "incomplete": "running",
                      "empty": "muted"}[summary.status]
        pill_text = {"complete": "Completed", "incomplete": "Resumable",
                     "empty": "No data"}[summary.status]
        self.pill = StatusPill(pill_text, pill_state)
        lay.addWidget(self.pill)

        prog_col = QVBoxLayout()
        prog_col.setSpacing(4)
        self.progress = widgets.AnimatedProgressBar(
            "completed" if summary.status == "complete" else "")
        self.progress.set_value_now(summary.progress_pct)
        self.progress.setFixedWidth(140)
        prog_col.addWidget(self.progress)
        self.pct_lbl = QLabel(f"{summary.progress_pct}%")
        self.pct_lbl.setProperty("muted", True)
        prog_col.addWidget(self.pct_lbl, 0, Qt.AlignHCenter)
        lay.addLayout(prog_col)

        self.resume_btn = widgets.icon_btn("play", "Resume this project")
        self.resume_btn.setEnabled(summary.status == "incomplete")
        self.resume_btn.clicked.connect(
            lambda: self.resume_requested.emit(self.summary))
        self.open_btn = widgets.icon_btn("external-link-alt", "Open folder")
        self.open_btn.clicked.connect(self._open_folder)
        lay.addWidget(self.resume_btn)
        lay.addWidget(self.open_btn)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def mouseDoubleClickEvent(self, event):        # noqa: N802 — Qt naming
        if event.button() == Qt.LeftButton:
            self.open_requested.emit(self.summary)
        super().mouseDoubleClickEvent(event)

    def _open_folder(self):
        os.startfile(self.summary.folder)            # noqa: S606 — user-initiated

    def _menu(self, pos):
        m = QMenu(self)
        s = self.summary
        resume = m.addAction("Resume")
        resume.setEnabled(s.status == "incomplete")
        m.addAction("Open folder")
        m.addSeparator()
        m.addAction("Export as .zip…")
        m.addAction("Rename…")
        m.addAction("Duplicate settings into new project…")
        m.addSeparator()
        dele = m.addAction("Delete…")
        chosen = m.exec(self.mapToGlobal(pos))
        if not chosen:
            return
        if chosen is resume:
            self.resume_requested.emit(s)
        elif chosen.text() == "Open folder":
            self._open_folder()
        elif chosen.text() == "Export as .zip…":
            self._export_zip()
        elif chosen.text() == "Rename…":
            self._rename()
        elif chosen.text().startswith("Duplicate"):
            self.duplicate_requested.emit(s)
        elif chosen is dele:
            self._delete()

    def _export_zip(self):
        base = os.path.join(os.path.expanduser("~"), "Downloads")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export project", os.path.join(base, self.summary.name + ".zip"),
            "Zip archive (*.zip)")
        if not path:
            return
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for dirpath, _dirs, files in os.walk(self.summary.folder):
                    for name in files:
                        full = os.path.join(dirpath, name)
                        zf.write(full, os.path.relpath(full, self.summary.folder))
            QMessageBox.information(self, "Website Downloader", "Export complete.")
        except OSError as exc:
            QMessageBox.warning(self, "Website Downloader", f"Export failed: {exc}")

    def _rename(self):
        from PySide6.QtWidgets import QInputDialog
        new, ok = QInputDialog.getText(self, "Rename project", "New name:",
                                       text=self.summary.name)
        if not ok or not new.strip() or new == self.summary.name:
            return
        try:
            target = os.path.join(os.path.dirname(self.summary.folder), new.strip())
            os.rename(self.summary.folder, target)
            self.changed.emit()
        except OSError as exc:
            QMessageBox.warning(self, "Website Downloader", f"Rename failed: {exc}")

    def _delete(self):
        ret = QMessageBox.question(
            self, "Website Downloader",
            f"Delete project '{self.summary.name}' and all its files?\n"
            "This cannot be undone.")
        if ret == QMessageBox.Yes:
            shutil.rmtree(self.summary.folder, ignore_errors=True)
            self.changed.emit()


class ProjectsView(QWidget):
    """Header (search + chips + sort + buttons) and the card list."""

    resume_requested = Signal(object)      # CrawlConfig
    new_requested = Signal()
    open_summary_requested = Signal(object)
    duplicate_requested = Signal(object)  # ProjectSummary -> prefilled wizard

    def __init__(self, projects_root: str, parent=None):
        super().__init__(parent)
        self.projects_root = projects_root
        self.summaries: list[ProjectSummary] = []
        self._filter_text = ""
        self._chip = CHIP_ALL
        self._sort = "last_run"
        self._loading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 12)
        lay.setSpacing(14)

        # header
        header = QHBoxLayout()
        title = QLabel("Projects")
        title.setProperty("heading", True)
        header.addWidget(title)
        header.addSpacing(16)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter projects…")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self._refilter)
        header.addWidget(self.search)

        self.chips: dict[str, QToolButton] = {}
        for chip, label in ((CHIP_ALL, "All"), (CHIP_RUNNING, "Resumable"),
                            (CHIP_DONE, "Completed"), (CHIP_FAILED, "With errors")):
            b = QToolButton()
            b.setText(label)
            b.setProperty("chip", True)
            b.setCheckable(True)
            b.setChecked(chip == CHIP_ALL)
            b.clicked.connect(lambda _c, c=chip: self._pick_chip(c))
            header.addWidget(b)
            self.chips[chip] = b
        header.addStretch(1)

        self.sort = QComboBox()
        self.sort.addItems(["Sort: Last run", "Sort: Name", "Sort: Status",
                            "Sort: Progress"])
        self.sort.currentIndexChanged.connect(lambda _i: self._refilter())
        header.addWidget(self.sort)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        new_btn = QPushButton("New Project")
        new_btn.setProperty("variant", "primary")
        new_btn.clicked.connect(self.new_requested.emit)
        header.addWidget(new_btn)
        lay.addLayout(header)

        # column caption row (spec §6)
        caption = QHBoxLayout()
        for text, stretch in (("PROJECT", 1), ("STATUS", 0), ("PROGRESS", 0),
                              ("", 0)):
            cap = QLabel(text)
            cap.setProperty("muted", True)
            f = cap.font(); f.setPointSize(9); f.setBold(True)
            cap.setFont(f)
            caption.addWidget(cap, stretch)
        caption.setSpacing(14)
        lay.addLayout(caption)

        # scrollable card list
        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 8, 0)
        self.list_lay.setSpacing(10)
        self.list_lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_host)
        scroll.setFrameShape(QFrame.NoFrame)
        lay.addWidget(scroll, stretch=1)

        self.empty = None
        self.error = None
        self.skeleton = None

    # ---------------------------------------------------------------- data

    def refresh(self):
        """Rescan; shows skeleton on first load, error state on failure."""
        first_load = not self.summaries and not self._loading
        if first_load:
            self._loading = True
            self._clear_list()
            self.skeleton = SkeletonRows(rows=4)
            self.list_lay.insertWidget(0, self.skeleton)
            QGuiApplication.processEvents()
        try:
            self.summaries = scan_projects(self.projects_root)
            self._error_state(None)
        except Exception as exc:                    # noqa: BLE001 — designed error state
            self._error_state(f"{type(exc).__name__}: {exc}")
        if self.skeleton is not None:
            self.skeleton.stop()
            self.skeleton.deleteLater()
            self.skeleton = None
        self._loading = False
        self._render()

    def _error_state(self, message: str | None):
        if message is None and self.error is not None:
            self.error.deleteLater()
            self.error = None
            return
        if message is None:
            return
        self._clear_list()
        self.error = EmptyState(
            "exclamation-triangle", "Couldn't read the projects folder",
            message, button_text="Retry",
            on_click=self.refresh)
        self.list_lay.insertWidget(0, self.error)

    def _clear_list(self):
        while self.list_lay.count() > 1:
            item = self.list_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _pick_chip(self, chip: str):
        self._chip = chip
        for key, b in self.chips.items():
            b.setChecked(key == chip)
        self._render()

    def _refilter(self):
        self._filter_text = self.search.text().strip().lower()
        self._render()

    def _visible(self, s: ProjectSummary) -> bool:
        if self._filter_text and not (self._filter_text in s.name.lower()
                                      or self._filter_text in s.seed.lower()):
            return False
        if self._chip == CHIP_RUNNING and s.status != "incomplete":
            return False
        if self._chip == CHIP_DONE and s.status != "complete":
            return False
        if self._chip == CHIP_FAILED and s.pages_failed == 0:
            return False
        return True

    def _render(self):
        self._clear_list()
        rows = [s for s in self.summaries if self._visible(s)]
        order = {"last_run": lambda s: (-(s.last_run_at or 0), s.name.lower()),
                 "name": lambda s: s.name.lower(),
                 "status": lambda s: (s.status != "incomplete", s.name.lower()),
                 "progress": lambda s: (-s.progress_pct, s.name.lower())}
        key_name = self.sort.currentText().replace("Sort: ", "").lower()
        rows.sort(key=order.get(key_name.replace(" ", "_"), order["last_run"]))

        if not rows and not self.summaries:
            self.empty = EmptyState(
                "folder-open", "No projects yet",
                "Create your first mirror — paste a URL and the crawler does "
                "the rest.", button_text="New Project",
                on_click=self.new_requested.emit)
            self.list_lay.insertWidget(0, self.empty)
            return
        if not rows:
            self.empty = EmptyState(
                "search", "Nothing matches",
                "Try clearing the filter or chips.", parent=self)
            self.list_lay.insertWidget(0, self.empty)
            return
        self.empty = None
        for s in rows:
            card = ProjectCard(s)
            card.resume_requested.connect(self._resume)
            card.open_requested.connect(self.open_summary_requested.emit)
            card.duplicate_requested.connect(self.duplicate_requested.emit)
            card.changed.connect(self.refresh)
            self.list_lay.insertWidget(self.list_lay.count() - 1, card)
            anim.fade_in(card, anim.DUR["enter"], slide_up=8)

    def _resume(self, summary: ProjectSummary):
        self.resume_requested.emit(config_for(summary))

    # ---- introspection helpers (used by the shell + tests) ------------------

    def _visible_first(self) -> ProjectSummary | None:
        rows = [s for s in self.summaries if self._visible(s)]
        rows.sort(key=lambda s: (-(s.last_run_at or 0), s.name.lower()))
        return rows[0] if rows else None

    def visible_cards(self) -> list[ProjectCard]:
        out = []
        for i in range(self.list_lay.count()):
            item = self.list_lay.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, ProjectCard):
                out.append(w)
        return out
