"""Screen 2 — New Project wizard (spec §7): six steps, numbered dot indicator
with animated fill, horizontal slide between steps, profiles as choice cards,
and a review step before Start."""
from __future__ import annotations

import os
import re

from PySide6.QtCore import QEasingCurve, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.types import CrawlConfig
from app.gui import anim, theme_engine, widgets
from app.gui.widgets import Card, SegmentedControl
from app.utils.url_utils import normalize

PROFILES = {
    "Blog": ("depth 3 · politeness 0.2 s", dict(depth=3, delay=0.2)),
    "Docs site": ("depth 6 · include subdomains", dict(depth=6, delay=0.2, allow_subdomains=True)),
    "E-commerce": ("skips cart / checkout / account", dict(
        depth=4, delay=0.2, exclude=("*cart*", "*checkout*", "*account*", "*login*"))),
    "Single page": ("depth 1 · just this page and its assets", dict(depth=1, delay=0.1)),
    "Custom": ("all fields manual — set them in the next steps", dict(depth=3, delay=0.2)),
}


class StepDots(QWidget):
    """Numbered dots whose fill animates as the wizard advances (spec §3/§7)."""

    def __init__(self, count: int, parent=None):
        super().__init__(parent)
        self.count = count
        self._fill = 0.0
        self.setFixedSize(count * 34, 20)
        self._anim = None

    def set_step(self, idx: int):
        if self._anim:
            self._anim.stop()
        if theme_engine.reduced_motion():
            self._fill = float(idx)
            self.update()
            return
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(200)
        self._anim.setStartValue(self._fill)
        self._anim.setEndValue(float(idx))
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(
            lambda v: (setattr(self, "_fill", v), self.update()))
        self._anim.start(QVariantAnimation.DeleteWhenStopped)

    def paintEvent(self, event):                     # noqa: N802 — Qt naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tok = theme_engine.tokens()
        for i in range(self.count):
            x = i * 34 + 10
            # completed dots are fully filled; the current one animates 0->1
            filled = max(0.0, min(1.0, self._fill - i))
            is_current = 0.0 <= self._fill - i < 1.0
            r = 8.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(tok["accent_tint"] if is_current else tok["border"]))
            p.drawEllipse(int(x - r), int(10 - r), int(2 * r), int(2 * r))
            if filled > 0:
                p.setBrush(QColor(tok["accent"]))
                p.setOpacity(filled)
                p.drawEllipse(int(x - r * 0.72), int(10 - r * 0.72),
                              int(2 * r * 0.72), int(2 * r * 0.72))
                p.setOpacity(1.0)
        p.end()


def _section(title: str) -> QLabel:
    lab = QLabel(title)
    lab.setProperty("cardtitle", True)
    return lab


class NewProjectWizard(QDialog):
    """Modal wizard (~640px). Slide transitions per spec §3; Start on step 6."""

    started = Signal(object)          # CrawlConfig

    def __init__(self, projects_root: str, seed_url: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.setFixedWidth(640)
        self.setMinimumHeight(520)
        self.projects_root = projects_root
        self._profile = "Custom"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 18)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("New Project")
        title.setProperty("heading", True)
        head.addWidget(title)
        head.addStretch(1)
        self.dots = StepDots(6)
        head.addWidget(self.dots, 0, Qt.AlignRight | Qt.AlignVCenter)
        outer.addLayout(head)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, stretch=1)

        nav = QHBoxLayout()
        self.back = QPushButton("Back")
        self.back.clicked.connect(self._go_back)
        self.next = QPushButton("Next")
        self.next.setProperty("variant", "primary")
        self.next.clicked.connect(self._go_next)
        nav.addStretch(1)
        nav.addWidget(self.back)
        nav.addWidget(self.next)
        outer.addLayout(nav)

        self._build_step1(seed_url)
        self._build_step2()
        self._build_step3()
        self._build_step4()
        self._build_step5()
        self._build_step6()
        self.stack.setCurrentIndex(0)
        self._sync_nav()

    # ------------------------------------------------------------- step 1: seed

    def _build_step1(self, seed_url: str):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("Where should we start?"))
        self.seed_edit = QLineEdit(seed_url)
        self.seed_edit.setPlaceholderText("https://example.com/   (drag a URL here)")
        self.seed_edit.setMinimumHeight(40)
        self.seed_edit.textChanged.connect(self._auto_name)
        lay.addWidget(self.seed_edit)
        lay.addWidget(QLabel("Project name:"))
        self.name_edit = QLineEdit()
        self.name_edit._touched = False
        # textEdited fires only for USER edits — auto-fill must not fight them
        self.name_edit.textEdited.connect(
            lambda: setattr(self.name_edit, "_touched", True))
        lay.addWidget(self.name_edit)
        self.error_label = QLabel("")
        self.error_label.setProperty("muted", True)
        lay.addWidget(self.error_label)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # drag a URL from a browser address bar straight onto the wizard (spec §5)
    def dragEnterEvent(self, event):                 # noqa: N802 — Qt naming
        if event.mimeData().hasText() or event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):                      # noqa: N802 — Qt naming
        md = event.mimeData()
        text = (md.text() if md.hasText()
                else (md.urls()[0].toString() if md.urls() else ""))
        if text:
            self.seed_edit.setText(text.strip())
            event.acceptProposedAction()

    def _auto_name(self):
        seed = normalize(self.seed_edit.text().strip())
        if seed and not self.name_edit._touched:
            host = seed.split("//", 1)[-1].split("/")[0]
            slug = re.sub(r"[^a-zA-Z0-9-]+", "-", host).strip("-").lower()
            self.name_edit.setText(slug or "project")

    # ---------------------------------------------------------- step 2: profile

    def _build_step2(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("What kind of site is it?"))
        self._profile_cards: dict[str, Card] = {}
        for name, (desc, _cfg) in PROFILES.items():
            card = Card()
            card.setProperty("choice", True)
            card.setCursor(Qt.PointingHandCursor)
            inner = QVBoxLayout(card)
            inner.setContentsMargins(16, 10, 16, 10)
            inner.setSpacing(2)
            t = QLabel(name)
            t.setProperty("cardtitle", True)
            inner.addWidget(t)
            d = QLabel(desc)
            d.setProperty("muted", True)
            inner.addWidget(d)
            lay.addWidget(card)
            self._profile_cards[name] = card
            card.mousePressEvent = (                      # noqa: N802 — closure capture
                lambda ev, n=name: self._pick_profile(n))
        lay.addStretch(1)
        self.stack.addWidget(page)

    def _pick_profile(self, name: str):
        self._profile = name
        for n, card in self._profile_cards.items():
            card.setProperty("selected", n == name)
            card.style().unpolish(card)
            card.style().polish(card)

    # ------------------------------------------------------- step 3: scope

    def _build_step3(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("Scope & limits"))

        depth_row = QHBoxLayout()
        depth_row.addWidget(QLabel("Max depth"))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 20)
        depth_row.addStretch(1)
        depth_row.addWidget(self.depth_spin)
        lay.addLayout(depth_row)

        lay.addWidget(QLabel("Scope"))
        self.scope = SegmentedControl(["Same host", "Include subdomains"])
        lay.addWidget(self.scope)

        lay.addWidget(QLabel("Exclude patterns (one per line, glob style)"))
        self.exclude_edit = QPlainTextEdit()
        self.exclude_edit.setPlaceholderText("*cart*\n*print=*")
        self.exclude_edit.setFixedHeight(84)
        lay.addWidget(self.exclude_edit)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Max file size (MB)"))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 4096)
        self.size_spin.setValue(50)
        size_row.addStretch(1)
        size_row.addWidget(self.size_spin)
        lay.addLayout(size_row)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # ------------------------------------------------------- step 4: network

    def _build_step4(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("Network"))

        conc_row = QHBoxLayout()
        conc_row.addWidget(QLabel("Concurrency"))
        self.conc_slider = QSlider(Qt.Horizontal)
        self.conc_slider.setRange(1, 32)
        self.conc_slider.setValue(8)
        self.conc_value = QLabel("8")
        self.conc_slider.valueChanged.connect(
            lambda v: self.conc_value.setText(str(v)))
        conc_row.addStretch(1)
        conc_row.addWidget(self.conc_value)
        conc_row.addWidget(self.conc_slider, stretch=2)
        lay.addLayout(conc_row)

        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("Per-host delay (s)"))
        self.delay_slider = QSlider(Qt.Horizontal)
        self.delay_slider.setRange(0, 2000)          # ms for smooth steps
        self.delay_slider.setValue(200)
        self.delay_value = QLabel("0.20")
        self.delay_slider.valueChanged.connect(
            lambda v: self.delay_value.setText(f"{v / 1000:.2f}"))
        delay_row.addStretch(1)
        delay_row.addWidget(self.delay_value)
        delay_row.addWidget(self.delay_slider, stretch=2)
        lay.addLayout(delay_row)

        self.robots_check = widgets._make_check("Respect robots.txt", True)
        self.verify_check = widgets._make_check("Verify offline links afterwards", True)
        self.search_check = widgets._make_check("Build full-text search index", True)
        self.headless_check = widgets._make_check(
            "Render pages with headless browser (JS-heavy sites)", False)
        for cb in (self.robots_check, self.verify_check, self.search_check,
                   self.headless_check):
            lay.addWidget(cb)

        lay.addWidget(QLabel("Proxies (one per line, http/https/socks5)"))
        self.proxies_edit = QPlainTextEdit()
        self.proxies_edit.setPlaceholderText("http://user:pass@proxy:8080")
        self.proxies_edit.setFixedHeight(64)
        lay.addWidget(self.proxies_edit)

        cookie_row = QHBoxLayout()
        cookie_row.addWidget(QLabel("Cookies"))
        self.cookie_edit = QLineEdit()
        self.cookie_edit.setPlaceholderText("session=abc123; token=xyz  (optional)")
        login_btn = QPushButton("Log in first…")
        login_btn.clicked.connect(self._capture_login)
        cookie_row.addWidget(self.cookie_edit, stretch=1)
        cookie_row.addWidget(login_btn)
        lay.addLayout(cookie_row)
        lay.addStretch(1)
        self.stack.addWidget(page)

    def _capture_login(self):
        from app.gui.login_capture import LoginDialog

        seed = self.seed_edit.text().strip()
        if not normalize(seed):
            self.error_label.setText("Enter the seed URL first — the login "
                                     "browser opens at that address.")
            self.stack.setCurrentIndex(0)
            return
        dlg = LoginDialog(seed, self)
        if dlg.exec() == QDialog.Accepted and dlg.cookie_header:
            self.cookie_edit.setText(dlg.cookie_header)

    # -------------------------------------------------------- step 5: output

    def _build_step5(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("Output"))

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Folder"))
        self.output_edit = QLineEdit(os.path.join(self.projects_root, "new-project"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        out_row.addWidget(self.output_edit, stretch=1)
        out_row.addWidget(browse)
        lay.addLayout(out_row)

        flat = widgets._make_check(
            "Flatten structure (all files in one folder)", False)
        flat.setEnabled(False)
        flat.setToolTip("Planned for a future release — mirrors always "
                        "preserve the site's folder structure today.")
        lay.addWidget(flat)
        lay.addStretch(1)
        self.stack.addWidget(page)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if d:
            self.output_edit.setText(d)

    # -------------------------------------------------------- step 6: review

    def _build_step6(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(10)
        lay.addWidget(_section("Review"))
        self.review = QLabel("")
        self.review.setWordWrap(True)
        self.review.setTextFormat(Qt.RichText)
        lay.addWidget(self.review)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # ---------------------------------------------------------------- nav

    def _slide_to(self, idx: int):
        """Horizontal slide: next enters from the right, previous from the
        left, ~200ms with fade (spec §3/§7)."""
        direction = 1 if idx > self.stack.currentIndex() else -1
        self.stack.setCurrentIndex(idx)
        page = self.stack.widget(idx)
        self.dots.set_step(idx)
        self._sync_nav()
        if theme_engine.reduced_motion():
            return
        var = QVariantAnimation(self)
        var.setDuration(200)
        var.setStartValue(float(direction * 40))
        var.setEndValue(0.0)
        var.setEasingCurve(QEasingCurve.OutCubic)

        def _setx(val):
            page.move(int(val), page.pos().y())

        var.valueChanged.connect(_setx)
        var.start(QVariantAnimation.DeleteWhenStopped)
        anim.fade_in(page, 200)

    def _go_next(self):
        if self.stack.currentIndex() == 0 and not self._validate_seed():
            return
        if self.stack.currentIndex() == self.stack.count() - 1:
            cfg = self.config()
            if cfg is not None:
                self.started.emit(cfg)
                self.accept()
            return
        if self.stack.currentIndex() == self.stack.count() - 2:
            self._render_review()
        self._slide_to(self.stack.currentIndex() + 1)

    def _go_back(self):
        if self.stack.currentIndex() > 0:
            self._slide_to(self.stack.currentIndex() - 1)

    def _sync_nav(self):
        idx = self.stack.currentIndex()
        self.back.setVisible(idx > 0)
        self.next.setText("Start" if idx == self.stack.count() - 1 else "Next")

    def _validate_seed(self) -> bool:
        seed = normalize(self.seed_edit.text().strip())
        if not seed:
            self.error_label.setText("Enter a valid http(s) URL to continue.")
            self.error_label.setProperty("muted", False)
            return False
        self.error_label.setText("")
        self.error_label.setProperty("muted", True)
        return True

    def _render_review(self):
        cfg = self.config()
        if cfg is None:
            return
        rows = [
            ("Seed", cfg.seed),
            ("Profile", self._profile),
            ("Depth", str(cfg.depth)),
            ("Scope", "subdomains included" if cfg.allow_subdomains else "same host"),
            ("Concurrency", str(cfg.concurrency)),
            ("Politeness delay", f"{cfg.delay:.2f} s"),
            ("Excludes", ", ".join(cfg.exclude) or "none"),
            ("Proxies", f"{len(cfg.proxies)} configured" if cfg.proxies else "none"),
            ("Headless rendering", "yes" if cfg.headless else "no"),
            ("Output", cfg.output_dir),
        ]
        self.review.setText("<br>".join(
            f"<span style='color:{theme_engine.tokens()['text2']}'>"
            f"{k}</span>&nbsp;&nbsp;<b>{v}</b>" for k, v in rows))

    # ---------------------------------------------------------------- config

    def config(self) -> CrawlConfig | None:
        seed = normalize(self.seed_edit.text().strip())
        if not seed:
            return None
        name = self.name_edit.text().strip()
        out = self.output_edit.text().strip()
        if name:
            out = os.path.join(os.path.dirname(out), name)
        # the chosen profile seeds the defaults; Custom reads the widgets
        profile = dict(PROFILES[self._profile][1])
        if self._profile == "Custom":
            depth = self.depth_spin.value()
            delay = self.delay_slider.value() / 1000
            allow_subdomains = self.scope.current() == 1
            exclude = tuple(
                ln.strip() for ln in self.exclude_edit.toPlainText().splitlines()
                if ln.strip())
        else:
            depth = profile["depth"]
            delay = profile["delay"]
            allow_subdomains = profile.get("allow_subdomains", False)
            exclude = tuple(profile.get("exclude", ()))
        proxies = tuple(
            ln.strip() for ln in self.proxies_edit.toPlainText().splitlines()
            if ln.strip())
        return CrawlConfig(
            seed=seed,
            output_dir=out,
            depth=depth,
            concurrency=self.conc_slider.value(),
            delay=delay,
            respect_robots=self.robots_check.isChecked(),
            allow_subdomains=allow_subdomains,
            verify_links=self.verify_check.isChecked(),
            build_search_index=self.search_check.isChecked(),
            exclude=exclude,
            proxies=proxies,
            headless=self.headless_check.isChecked(),
            cookies=tuple(c for c in (self.cookie_edit.text().strip(),) if c),
            state_db=os.path.join(out, "project.db"),
        )
