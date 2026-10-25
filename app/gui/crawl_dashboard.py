"""The live crawl dashboard: one page of the main window."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.projects import ProjectSummary
from app.core.types import CrawlConfig
from app.gui.worker import ProjectWorker
from app.utils.url_utils import normalize


class NewProjectDialog(QDialog):
    """Phase-4 wizard fields: seed, depth, concurrency, output folder (+ the
    politeness/scope options people actually reach for)."""

    def __init__(self, parent=None, default_output: str = "mirror") -> None:
        super().__init__(parent)
        self.setWindowTitle("New Project")
        form = QFormLayout(self)

        self.seed_edit = QLineEdit(self)
        self.seed_edit.setPlaceholderText("https://example.com/")
        form.addRow("Seed URL", self.seed_edit)

        self.depth_spin = QSpinBox(self, minimum=0, maximum=20, value=3)
        form.addRow("Max depth", self.depth_spin)

        self.concurrency_spin = QSpinBox(self, minimum=1, maximum=64, value=8)
        form.addRow("Concurrency", self.concurrency_spin)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(default_output, self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(browse)
        form.addRow("Output folder", out_row)

        self.delay_spin = QDoubleSpinBox(self, minimum=0.0, maximum=10.0,
                                         singleStep=0.05, value=0.2, suffix=" s")
        form.addRow("Per-host delay", self.delay_spin)

        self.robots_check = QCheckBox("Respect robots.txt", self)
        self.robots_check.setChecked(True)
        self.subdomains_check = QCheckBox("Include subdomains", self)
        self.verify_check = QCheckBox("Verify offline links afterwards", self)
        self.verify_check.setChecked(True)
        extras = QVBoxLayout()
        extras.addWidget(self.robots_check)
        extras.addWidget(self.subdomains_check)
        extras.addWidget(self.verify_check)
        scope_box = QGroupBox("Politeness & scope")
        scope_box.setLayout(extras)
        form.addRow(scope_box)

        self.proxies_edit = QTextEdit(self)
        self.proxies_edit.setPlaceholderText("http://user:pass@proxy:8080   (one per line, optional)")
        self.proxies_edit.setFixedHeight(60)
        form.addRow("Proxies", self.proxies_edit)

        # ---- phase 7: JS-heavy sites & login-gated content
        self.headless_check = QCheckBox("Render pages with headless browser (JS-heavy sites)", self)
        form.addRow(self.headless_check)

        cookie_row = QHBoxLayout()
        self.cookie_edit = QLineEdit(self)
        self.cookie_edit.setPlaceholderText("session=abc123; token=xyz  (optional)")
        login_btn = QPushButton("Log in first…", self)
        login_btn.clicked.connect(self._capture_login)
        cookie_row.addWidget(self.cookie_edit)
        cookie_row.addWidget(login_btn)
        form.addRow("Cookies", cookie_row)

        buttons = QHBoxLayout()
        ok = QPushButton("Start", self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel", self)
        cancel.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        form.addRow(buttons)

        self.error_label = QLabel("", self)
        self.error_label.setStyleSheet("color: #d32f2f;")
        form.addRow(self.error_label)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if d:
            self.output_edit.setText(d)

    def config(self) -> CrawlConfig | None:
        seed = normalize(self.seed_edit.text().strip())
        if not seed:
            # inline, non-blocking: modal boxes stall offscreen/automation
            self.error_label.setText("Enter a valid http(s) seed URL.")
            return None
        self.error_label.setText("")
        out = self.output_edit.text().strip() or "mirror"
        proxies = tuple(
            ln.strip() for ln in self.proxies_edit.toPlainText().splitlines()
            if ln.strip()
        )
        return CrawlConfig(
            seed=seed,
            output_dir=out,
            depth=self.depth_spin.value(),
            concurrency=self.concurrency_spin.value(),
            delay=float(self.delay_spin.value()),
            respect_robots=self.robots_check.isChecked(),
            allow_subdomains=self.subdomains_check.isChecked(),
            verify_links=self.verify_check.isChecked(),
            proxies=proxies,
            state_db=os.path.join(out, "project.db"),
            headless=self.headless_check.isChecked(),
            cookies=tuple(c for c in (self.cookie_edit.text().strip(),) if c),
        )

    def _capture_login(self) -> None:
        from app.gui.login_capture import LoginDialog

        seed = self.seed_edit.text().strip()
        if not seed:
            self.error_label.setText("Enter the seed URL first — the login "
                                     "browser opens at that address.")
            return
        dlg = LoginDialog(seed, self)
        if dlg.exec() == QDialog.Accepted and dlg.cookie_header:
            self.cookie_edit.setText(dlg.cookie_header)


class CrawlDashboard(QWidget):
    """Progress bar, live counters, page/error lists — fed only by signals."""

    def __init__(self) -> None:
        super().__init__()
        self.worker: ProjectWorker | None = None
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self.pause_btn = QPushButton("Pause", self, enabled=False)
        self.pause_btn.clicked.connect(self.pause)
        self.resume_btn = QPushButton("Resume", self, enabled=False)
        self.resume_btn.clicked.connect(self.resume)
        self.cancel_btn = QPushButton("Cancel", self, enabled=False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.open_btn = QPushButton("Open Output Folder", self, enabled=False)
        self.open_btn.clicked.connect(self.open_output)
        for b in (self.pause_btn, self.resume_btn, self.cancel_btn, self.open_btn):
            controls.addWidget(b)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        stats_box = QGroupBox("Live statistics")
        grid_rows: list[tuple[str, str]] = []
        from PySide6.QtWidgets import QGridLayout
        grid = QGridLayout(stats_box)
        self.labels: dict[str, QLabel] = {}
        for i, key in enumerate((
            "pages", "assets", "speed", "bytes",
            "out_of_scope", "state", "output",
        )):
            grid.addWidget(QLabel(key.replace("_", " ").title() + ":"), i // 3, (i % 3) * 2)
            lab = QLabel("—", self)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.labels[key] = lab
            grid.addWidget(lab, i // 3, (i % 3) * 2 + 1)
        layout.addWidget(stats_box)

        lists = QHBoxLayout()
        pages_box = QGroupBox("Pages saved")
        pages_lay = QVBoxLayout(pages_box)
        self.pages_list = QListWidget(self)
        pages_lay.addWidget(self.pages_list)
        errors_box = QGroupBox("Errors & skips")
        errors_lay = QVBoxLayout(errors_box)
        self.errors_list = QListWidget(self)
        errors_lay.addWidget(self.errors_list)
        lists.addWidget(pages_box)
        lists.addWidget(errors_box)
        layout.addLayout(lists, stretch=1)

        # ---- full-text search over the finished mirror (phase 6)
        search_box = QGroupBox("Search saved pages (press Enter)")
        search_lay = QVBoxLayout(search_box)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("search the offline copy — e.g. deployment guide")
        self.search_edit.returnPressed.connect(self._run_search)
        self.search_results = QListWidget(self)
        self.search_results.itemDoubleClicked.connect(self._open_search_hit)
        search_lay.addWidget(self.search_edit)
        search_lay.addWidget(self.search_results)
        search_box.setMaximumHeight(180)
        layout.addWidget(search_box)
        self._search_hits: list = []
        self.current_output: str = ""

        self._set_state("Ready")

    # ---- running a project -------------------------------------------------

    def start_project(self, cfg: CrawlConfig) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "Website Downloader",
                                    "A project is already running — cancel it first.")
            return
        self.pages_list.clear()
        self.errors_list.clear()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.labels["output"].setText(cfg.output_dir)
        self.current_output = cfg.output_dir
        self.worker = ProjectWorker(cfg)
        w = self.worker
        w.page_downloaded.connect(self._on_page)
        w.asset_downloaded.connect(self._on_asset)
        w.progress_updated.connect(self._on_progress)
        w.error_occurred.connect(self._on_error)
        w.state_changed.connect(self._set_state)
        w.run_finished.connect(self._on_finished)
        w.start()
        self.pause_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.new_project_allowed = True
        self.open_btn.setEnabled(True)

    def show_project_summary(self, summary: ProjectSummary) -> None:
        """Read-only view of a saved project (double-click on the panel)."""
        self.pages_list.clear()
        self.errors_list.clear()
        self.current_output = summary.folder
        self.labels["output"].setText(summary.folder)
        self.labels["pages"].setText(f"{summary.pages_done} saved")
        self.labels["assets"].setText(f"{summary.assets_done} saved")
        self.labels["bytes"].setText("—")
        self.labels["speed"].setText("—")
        self.labels["out_of_scope"].setText(str(summary.urls_total - summary.pages_done))
        self._set_state(f"Viewing saved project — {summary.status_display}")
        self.progress.setRange(0, 100)
        self.progress.setValue(summary.progress_pct)

    # ---- slots ---------------------------------------------------------------

    def _on_page(self, url: str, path: str) -> None:
        self.pages_list.insertItem(0, path or url)
        if self.pages_list.count() > 400:
            self.pages_list.takeItem(self.pages_list.count() - 1)

    def _on_asset(self, url: str, path: str) -> None:
        pass  # the counters cover assets; keep the page list signal-only

    def _on_progress(self, payload: dict) -> None:
        pages_seen = max(payload.get("pages_seen", 0), 1)
        done = payload.get("pages_saved", 0) + payload.get("pages_failed", 0)
        self.progress.setRange(0, max(pages_seen, done))
        self.progress.setValue(done)
        self.labels["pages"].setText(
            f"{payload.get('pages_saved', 0)} saved / {payload.get('pages_failed', 0)} failed"
            f" ({payload.get('pages_seen', 0)} seen)")
        self.labels["assets"].setText(
            f"{payload.get('assets_saved', 0)} saved / {payload.get('assets_failed', 0)} failed"
            f" ({payload.get('assets_seen', 0)} seen)")
        self.labels["speed"].setText(f"{payload.get('mbps_recent', 0):.2f} MB/s")
        mb = payload.get("bytes", 0) / 1_000_000
        self.labels["bytes"].setText(f"{mb:.2f} MB")
        self.labels["out_of_scope"].setText(str(payload.get("out_of_scope", 0)))
        if payload.get("cancelled"):
            self._set_state("Cancelling")

    def _on_error(self, msg: str) -> None:
        self.errors_list.insertItem(0, msg)
        if self.errors_list.count() > 400:
            self.errors_list.takeItem(self.errors_list.count() - 1)

    def _on_finished(self, report) -> None:
        s = report.stats
        self.labels["pages"].setText(
            f"{s.pages_saved} saved / {s.pages_failed} failed ({s.pages_seen} seen)")
        self.labels["assets"].setText(
            f"{s.assets_saved} saved / {s.assets_failed} failed ({s.assets_seen} seen)")
        self._set_state("Done" if not report.failures else
                        f"Done ({len(report.failures)} issues — see errors)")
        self.pause_btn.setEnabled(False)
        self.resume_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.open_btn.setEnabled(True)
        for f in report.failures[:50]:
            self._on_error(f"[{f.stage}] {f.url} -> {f.error}")

    def _set_state(self, state: str) -> None:
        if state == "done":
            return          # _on_finished renders the terminal state itself
        display = {
            "running": "Running", "paused": "Paused", "resumed": "Running",
            "cancelling": "Cancelling", "failed": "Failed",
        }.get(state, state)
        self.labels["state"].setText(display)
        paused = display == "Paused"
        self.pause_btn.setEnabled(not paused and self.worker is not None
                                  and self.worker.isRunning())
        self.resume_btn.setEnabled(paused)

    # ---- controls -------------------------------------------------------------

    def pause(self) -> None:
        if self.worker is not None:
            self.worker.pause()

    def resume(self) -> None:
        if self.worker is not None:
            self.worker.resume()

    def cancel(self) -> None:
        if self.worker is None:
            return
        ret = QMessageBox.question(
            self, "Website Downloader",
            "Cancel the current crawl? Completed files are kept and the "
            "project stays resumable.")
        if ret == QMessageBox.Yes:
            self.worker.cancel()

    def open_output(self) -> None:
        out = self.labels["output"].text()
        if out and out != "—":
            os.startfile(out)  # noqa: S606 — windows explorer; user-initiated

    # ---- saved-pages search (phase 6) ------------------------------------------

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
                os.startfile(page)  # noqa: S606 — local html; user-initiated
