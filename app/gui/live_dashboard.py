"""Screen 3 — Live dashboard (spec §8): top bar with pulsing status pill,
four count-up stat cards, one large animated progress bar, and a split lower
area — site-map tree (left) and a color-coded, filterable, auto-scrolling log
panel (right). The saved-pages search box (phase 6) lives here too."""
from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.projects import ProjectSummary
from app.core.types import CrawlConfig
from app.gui import anim, theme_engine, widgets
from app.gui.widgets import AnimatedProgressBar, Card, SegmentedControl, StatusPill

LOG_INFO, LOG_WARN, LOG_ERROR = "info", "warning", "error"


class LogPanel(QGroupBox):
    """Scrollable, filterable, color-coded log (spec §8): monospace, newest at
    the bottom, auto-scrolls unless the user scrolled up."""

    def __init__(self, parent=None):
        super().__init__("Activity log", parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 14, 10, 10)
        lay.setSpacing(8)

        self._lines: list[tuple[str, str]] = []      # (severity, text)
        self._severity_of_row: list[str] = []

        bar = QHBoxLayout()
        self.filter = SegmentedControl(["All", "Warnings", "Errors"])
        self.filter.changed.connect(lambda _i: self._rerender())
        bar.addWidget(self.filter)
        bar.addStretch(1)
        self.export_btn = widgets.icon_btn("save", "Save the log to a file")
        self.export_btn.clicked.connect(self._export)
        self.clear_btn = widgets.icon_btn("trash", "Clear the log")
        self.clear_btn.clicked.connect(self.clear)
        bar.addWidget(self.export_btn)
        bar.addWidget(self.clear_btn)
        self.count_lbl = QLabel("0 lines")
        self.count_lbl.setProperty("muted", True)
        bar.addWidget(self.count_lbl)
        lay.addLayout(bar)

        self.view = QListWidget()
        self.view.setObjectName("LogView")
        self.view.setUniformItemSizes(True)
        lay.addWidget(self.view, stretch=1)
        self._user_scrolled_up = False
        sb = self.view.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll)
        sb.rangeChanged.connect(lambda _lo, hi: self._autoscroll(hi))

    def _on_scroll(self, value):
        sb = self.view.verticalScrollBar()
        self._user_scrolled_up = value < sb.maximum() - 8

    def _autoscroll(self, _hi):
        if not self._user_scrolled_up:
            self.view.scrollToBottom()

    def add(self, severity: str, text: str):
        self._lines.append((severity, text))
        if self._passes(severity):
            self._append_row(severity, text)
        self.count_lbl.setText(f"{len(self._lines)} lines")
        sb = self.view.verticalScrollBar()
        self._autoscroll(sb.maximum())

    def _passes(self, severity: str) -> bool:
        mode = self.filter.current()
        return mode == 0 or (mode == 1 and severity != LOG_INFO) or (mode == 2 and severity == LOG_ERROR)

    def _append_row(self, severity: str, text: str):
        tok = theme_engine.tokens()
        color = {LOG_INFO: tok["text2"], LOG_WARN: tok["warning"],
                  LOG_ERROR: tok["danger"]}[severity]
        item = QListWidgetItem(text)
        item.setForeground(QColor(color))
        self.view.addItem(item)
        self._severity_of_row.append(severity)
        if self.view.count() > 2000:
            self.view.takeItem(0)
            self._severity_of_row.pop(0)

    def _rerender(self):
        self.view.clear()
        self._severity_of_row = []
        for severity, text in self._lines:
            if self._passes(severity):
                self._append_row(severity, text)
        self.view.scrollToBottom()

    def clear(self):
        self._lines.clear()
        self._severity_of_row.clear()
        self.view.clear()
        self.count_lbl.setText("0 lines")

    def _export(self):
        if not self._lines:
            return
        import os
        from PySide6.QtWidgets import QFileDialog
        default = os.path.join(os.path.expanduser("~"), "Downloads",
                               "crawl-log.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Save activity log",
                                              default, "Text file (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for severity, text in self._lines:
                    fh.write(f"[{severity:>7}] {text}\n")
        except OSError:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Website Downloader",
                                "Could not write the log file.")


class LiveDashboard(QWidget):
    """Feeds exclusively from ProjectWorker signals (never blocks the GUI)."""

    def __init__(self) -> None:
        super().__init__()
        self.worker = None
        self.current_output = ""
        self._last_pages = self._last_assets = self._last_speed_x10 = 0
        self._pill_pulse = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 12)
        lay.setSpacing(14)

        # ---- top bar: name + pulsing pill + controls
        top = QHBoxLayout()
        self.labels: dict[str, QLabel] = {"state": StatusPill("Idle", "muted")}
        top.addWidget(self.labels["state"])
        top.addStretch(1)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.pause)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setEnabled(False)
        self.resume_btn.clicked.connect(self.resume)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("variant", "danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = widgets.icon_btn("folder", "Open output folder")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_output)
        self.preview_btn = widgets.icon_btn("globe", "Preview in browser")
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self._emit_preview)
        for b in (self.pause_btn, self.resume_btn, self.cancel_btn,
                  self.open_btn, self.preview_btn):
            top.addWidget(b)
        lay.addLayout(top)

        # ---- stat cards (count-up numbers per spec §8)
        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_pages = widgets.StatCard("Pages crawled", "file-alt")
        self.stat_assets = widgets.StatCard("Assets downloaded", "image")
        self.stat_speed = widgets.StatCard("Speed", "tachometer-alt")
        self.stat_eta = widgets.StatCard("ETA", "hourglass-half")
        for card in (self.stat_pages, self.stat_assets, self.stat_speed,
                     self.stat_eta):
            stats.addWidget(card, stretch=1)
        lay.addLayout(stats)

        # ---- big progress + status line
        self.progress = AnimatedProgressBar()
        self.progress.setFixedHeight(10)
        lay.addWidget(self.progress)
        status_row = QHBoxLayout()
        self.labels["pages"] = QLabel("—")
        self.labels["pages"].setProperty("muted", True)
        self.labels["output"] = QLabel("—")
        self.labels["output"].setProperty("muted", True)
        status_row.addWidget(self.labels["pages"])
        status_row.addStretch(1)
        status_row.addWidget(self.labels["output"])
        lay.addLayout(status_row)

        # ---- speed history sparkline (rolls with every progress tick)
        speed_box = widgets.Card()
        speed_lay = QVBoxLayout(speed_box)
        speed_lay.setContentsMargins(14, 8, 14, 8)
        cap = QLabel("Download speed history (MB/s)")
        cap.setProperty("muted", True)
        speed_lay.addWidget(cap)
        self.sparkline = widgets.Sparkline(samples=90)
        speed_lay.addWidget(self.sparkline)
        lay.addWidget(speed_box)

        # ---- split: site-map tree + log
        split = QHBoxLayout()
        split.setSpacing(12)
        tree_box = QGroupBox("Site map")
        tree_lay = QVBoxLayout(tree_box)
        tree_lay.setContentsMargins(10, 14, 10, 10)
        self.pages_list = QTreeWidget()          # pages tree; list-shaped alias
        self.pages_list.setHeaderHidden(True)
        self._tree_nodes: dict[str, QTreeWidgetItem] = {}
        tree_lay.addWidget(self.pages_list)
        self.log = LogPanel()
        split.addWidget(tree_box, stretch=3)
        split.addWidget(self.log, stretch=2)
        lay.addLayout(split, stretch=1)

        # ---- errors bucket (kept for report-time failures)
        self._errors_host = QWidget()
        self._errors_host.setVisible(False)

        # ---- saved-pages search (phase 6 lives on the completed view)
        search_box = QGroupBox("Search saved pages (press Enter)")
        s_lay = QVBoxLayout(search_box)
        s_lay.setContentsMargins(10, 14, 10, 10)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("search the offline copy — e.g. deployment guide")
        self.search_edit.returnPressed.connect(self._run_search)
        self.search_results = QListWidget()
        self.search_results.itemDoubleClicked.connect(self._open_search_hit)
        s_lay.addWidget(self.search_edit)
        s_lay.addWidget(self.search_results)
        search_box.setMaximumHeight(170)
        lay.addWidget(search_box)
        self._search_hits: list = []

    # ------------------------------------------------------------- lifecycle

    def start_project(self, cfg: CrawlConfig, worker_factory) -> None:
        """worker_factory(cfg) -> ProjectWorker; kept as a callable so the
        shell controls construction and the dashboard stays dumb."""
        if self.worker is not None and self.worker.isRunning():
            return
        self.current_output = cfg.output_dir
        self.labels["output"].setText(cfg.output_dir)
        self.log.clear()
        self.pages_list.clear()
        self._tree_nodes.clear()
        self.progress.set_value_now(0)
        self.progress.setProperty("status", "")
        self.stat_pages._last = self.stat_assets._last = 0
        self._last_speed_x10 = 0
        worker = worker_factory(cfg)
        self.worker = worker
        worker.page_event.connect(self._on_page_event)
        worker.page_downloaded.connect(self._on_page)
        worker.asset_downloaded.connect(self._on_asset)
        worker.progress_updated.connect(self._on_progress)
        worker.error_occurred.connect(self._on_error)
        worker.state_changed.connect(self._on_state)
        worker.run_finished.connect(self._on_finished)
        worker.start()
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)
        self._on_state("running")

    def show_project_summary(self, summary: ProjectSummary) -> None:
        """Read-only view of a saved project (double-click on a card)."""
        self.current_output = summary.folder
        self.labels["output"].setText(summary.folder)
        self.labels["pages"].setText(f"{summary.pages_done:,} pages · "
                                     f"{summary.assets_done:,} assets")
        self.labels["state"].set_state(
            "completed" if summary.status == "complete" else "muted",
            "Completed" if summary.status == "complete" else "Saved project")
        self.progress.set_value_now(summary.progress_pct)
        self.stat_pages.set_value(summary.pages_done)
        self.stat_assets.set_value(summary.assets_done)
        self.stat_speed.set_value(0)
        self.stat_eta.set_value(0)
        self.log.clear()
        self.pages_list.clear()
        self._tree_nodes.clear()
        self._seed_root(summary)
        self.log.add(LOG_INFO, f"viewing saved project: {summary.name}")
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.open_btn.setEnabled(True)
        self.preview_btn.setEnabled(True)

    def _seed_root(self, summary: ProjectSummary):
        root = QTreeWidgetItem([summary.name])
        self.pages_list.addTopLevelItem(root)
        self._tree_nodes = {}

    # ------------------------------------------------------------- slots

    def _on_state(self, state: str):
        display = {"running": "Running", "paused": "Paused", "resumed": "Running",
                   "cancelling": "Cancelling", "failed": "Failed",
                   "done": None, "paused_view": None}.get(state)
        if display is None:
            return                          # run_finished renders the terminal state
        pill_state = ("running" if state in ("running", "resumed")
                      else "paused" if state == "paused"
                      else "failed" if state == "failed" else "muted")
        self.labels["state"].set_state(pill_state, display)
        if pill_state == "running" and not theme_engine.reduced_motion():
            self._start_pulse()
        else:
            self._stop_pulse()
        paused = pill_state == "paused"
        self.pause_btn.setEnabled(not paused and self.worker is not None
                                  and self.worker.isRunning())
        self.resume_btn.setEnabled(paused)

    def _start_pulse(self):
        self._pill_pulse = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._toggle_pulse)
        self._pulse_timer.start(900)

    def _stop_pulse(self):
        t = getattr(self, "_pulse_timer", None)
        if t is not None:
            t.stop()
            t.deleteLater()
            self._pulse_timer = None
        self.labels["state"].setProperty("pulse", False)

    def _toggle_pulse(self):
        self._pill_pulse = not self._pill_pulse
        self.labels["state"].setProperty("pulse", self._pill_pulse)
        self.labels["state"].style().unpolish(self.labels["state"])
        self.labels["state"].style().polish(self.labels["state"])

    def _on_page(self, url: str, path: str) -> None:
        self.log.add(LOG_INFO, f"saved {path or url}")

    def _on_page_event(self, payload: dict) -> None:
        """Full per-page payload (url, path, referer) -> site-map tree node."""
        url = payload.get("url", "")
        path = payload.get("path", "")
        referer = payload.get("referer")
        parent = self._tree_nodes.get(referer) if referer else None
        if parent is None and self.pages_list.topLevelItemCount():
            parent = self.pages_list.topLevelItem(0)
        label = os.path.basename(path) if path else url
        item = QTreeWidgetItem([label])
        item.setToolTip(0, url)
        if parent is not None:
            parent.addChild(item)
            parent.setExpanded(True)
        else:
            self.pages_list.addTopLevelItem(item)
        self._tree_nodes[url] = item

    def _on_asset(self, url: str, path: str) -> None:
        self.log.add(LOG_INFO, f"asset {path or url}")

    def _on_progress(self, payload: dict) -> None:
        pages_seen = max(payload.get("pages_seen", 0), 1)
        done = payload.get("pages_saved", 0) + payload.get("pages_failed", 0)
        pct = int(done * 100 / max(pages_seen, done, 1))
        self.progress.animate_to(pct)

        self.stat_pages.set_value(payload.get("pages_saved", 0))
        self.stat_assets.set_value(payload.get("assets_saved", 0))
        speed_x10 = int(payload.get("mbps_recent", 0) * 10)
        if speed_x10 != self._last_speed_x10:
            anim.count_up(self.stat_speed.number, self._last_speed_x10, speed_x10,
                          fmt="{:.1f}")
            self._last_speed_x10 = speed_x10
        self.sparkline.push(payload.get("mbps_recent", 0))
        # ETA from the recent page rate
        remaining = pages_seen - done
        elapsed = payload.get("elapsed_s", 0.1) or 0.1
        rate = done / elapsed if elapsed else 0
        eta_s = int(remaining / rate) if rate > 0.05 and remaining > 0 else 0
        self.stat_eta.set_value(eta_s, fmt="{}s")

        self.labels["pages"].setText(
            f"{payload.get('pages_saved', 0):,} pages saved · "
            f"{payload.get('pages_failed', 0)} failed · "
            f"{payload.get('assets_saved', 0):,} assets")
        if payload.get("paused"):
            self._on_state("paused")
        if payload.get("cancelled"):
            self._on_state("cancelling")

    def _on_error(self, msg: str) -> None:
        self.log.add(LOG_ERROR, msg)

    def _on_finished(self, report) -> None:
        s = report.stats
        self.progress.celebrate()
        self.labels["state"].set_state(
            "completed" if not report.failures else "failed",
            "Completed" if not report.failures
            else f"Done ({len(report.failures)} issues)")
        self._stop_pulse()
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        for f in report.failures[:50]:
            self.log.add(LOG_ERROR, f"[{f.stage}] {f.url} -> {f.error}")
        self.log.add(LOG_INFO,
                     f"finished: {s.pages_saved} pages, {s.assets_saved} assets, "
                     f"{s.bytes_downloaded / 1_000_000:.2f} MB in {s.elapsed:.1f}s")

    # ------------------------------------------------------------- controls

    def pause(self) -> None:
        if self.worker is not None:
            self.worker.pause()

    def resume(self) -> None:
        if self.worker is not None:
            self.worker.resume()

    def cancel(self) -> None:
        if self.worker is None:
            return
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "Website Downloader",
            "Cancel the current crawl? Completed files are kept and the "
            "project stays resumable.")
        if ret == QMessageBox.Yes:
            self.worker.cancel()

    def open_output(self) -> None:
        out = self.labels["output"].text()
        if out and out != "—":
            os.startfile(out)              # noqa: S606 — user-initiated

    preview_requested = None

    def _emit_preview(self):
        if self.preview_requested is not None:
            self.preview_requested()

    # ------------------------------------------------------------- search

    def _run_search(self) -> None:
        from app.core.search_index import open_ro

        self.search_results.clear()
        self._search_hits = []
        query = self.search_edit.text().strip()
        if not query or not self.current_output:
            return
        db = os.path.join(self.current_output, "search.db")
        if not os.path.isfile(db):
            self.search_results.addItem("no search index in this project")
            return
        index = open_ro(db)
        try:
            hits = index.search(query)
        finally:
            index.close()
        self._search_hits = hits
        if not hits:
            self.search_results.addItem("no pages matched")
            return
        for h in hits:
            self.search_results.addItem(f"{h.title} — {h.snippet}")

    def _open_search_hit(self, item) -> None:
        row = self.search_results.row(item)
        if 0 <= row < len(self._search_hits):
            page = os.path.join(self.current_output,
                                *self._search_hits[row].path.split("/"))
            if os.path.isfile(page):
                os.startfile(page)          # noqa: S606 — user-initiated
