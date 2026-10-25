"""Screen 4 — Settings (spec §9): grouped rows (label left, control right,
~44px), dividers, live-applied theme swatches and accent picker — no Apply
button. Persists via QSettings handled by the shell."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QFrame,
    QVBoxLayout,
    QWidget,
)

from app.gui import theme_engine, widgets
from app.gui.widgets import Card

SHORTCUTS = [
    ("Ctrl + K", "Command palette"),
    ("Ctrl + N", "New project"),
    ("Ctrl + R", "Refresh projects"),
    ("Ctrl + ,", "Open settings"),
    ("Space", "Pause / resume the running project"),
    ("Esc", "Close dialogs and the palette"),
]


def _swatch_pixmap(theme: str) -> QPixmap:
    """Mini live-preview: background strip, surface block, accent bar.
    'system' previews whatever the OS currently resolves to."""
    tok = theme_engine.THEMES[theme_engine.resolve(theme)]
    pm = QPixmap(64, 40)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(tok["bg"]))
    p.drawRoundedRect(0, 0, 64, 40, 8, 8)
    p.setBrush(QColor(tok["surface"]))
    p.drawRoundedRect(6, 6, 34, 28, 6, 6)
    p.setBrush(QColor(tok["accent"]))
    p.drawRoundedRect(46, 12, 10, 16, 4, 4)
    p.end()
    return pm


def _accent_pixmap(color: str) -> QPixmap:
    pm = QPixmap(26, 26)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(2, 2, 22, 22)
    p.end()
    return pm


class SettingsRow(QWidget):
    """One setting: label (+ optional description) left, control right."""

    def __init__(self, label: str, control: QWidget, description: str = ""):
        super().__init__()
        self.setFixedHeight(44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        lab = QLabel(label)
        text_col.addWidget(lab)
        if description:
            d = QLabel(description)
            d.setProperty("muted", True)
            text_col.addWidget(d)
        lay.addLayout(text_col, stretch=1)
        lay.addWidget(control)
        divider = QFrame()
        divider.setObjectName("Divider")
        self.divider = divider


class Section(Card):
    def __init__(self, title: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(12)
        head = QLabel(title)
        head.setProperty("cardtitle", True)
        lay.addWidget(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(0)
        lay.addLayout(self.body)

    def add_row(self, label: str, control: QWidget, description: str = "") -> SettingsRow:
        row = SettingsRow(label, control, description)
        self.body.addWidget(row)
        line = QFrame()
        line.setObjectName("Divider")
        line.setFixedHeight(1)
        self.body.addWidget(line)
        return row


class SettingsView(QWidget):
    theme_changed = Signal(str)           # light/dark/midnight/high_contrast/system
    accent_changed = Signal(str)
    general_changed = Signal(dict)       # {projects_root, start_minimized}

    def __init__(self, current_theme: str, current_accent: str,
                 projects_root: str, start_minimized: bool = False, parent=None):
        super().__init__(parent)
        self._theme = current_theme
        self._accent = current_accent

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 12)
        outer.setSpacing(14)
        title = QLabel("Settings")
        title.setProperty("heading", True)
        outer.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 12, 0)
        lay.setSpacing(14)
        scroll.setWidget(host)
        outer.addWidget(scroll, stretch=1)

        # ---- Appearance
        appearance = Section("Appearance")
        self._theme_buttons: dict[str, QPushButton] = {}
        theme_widget = QWidget()
        theme_strip = QHBoxLayout(theme_widget)
        theme_strip.setContentsMargins(0, 0, 0, 0)
        theme_strip.setSpacing(8)
        for name in ("light", "dark", "midnight", "high_contrast", "system"):
            b = QPushButton()
            b.setFixedSize(72, 48)
            pm = _swatch_pixmap(name)
            b.setIcon(pm)
            b.setIconSize(pm.size())
            b.setProperty("swatch", True)
            b.setToolTip({"light": "Light", "dark": "Dark",
                          "midnight": "Midnight (OLED)",
                          "high_contrast": "High contrast",
                          "system": "Follow the OS"}[name])
            b.clicked.connect(lambda _c, n=name: self._pick_theme(n))
            theme_strip.addWidget(b)
            self._theme_buttons[name] = b
        theme_strip.addStretch(1)
        appearance.add_row("Theme", theme_widget,
                           "Switching applies instantly — no restart needed")
        # accent picker row
        accent_widget = QWidget()
        accent_lay = QHBoxLayout(accent_widget)
        accent_lay.setContentsMargins(0, 0, 0, 0)
        accent_lay.setSpacing(8)
        self._accent_buttons: dict[str, QPushButton] = {}
        for name, color in theme_engine.ACCENTS.items():
            b = QPushButton()
            b.setFixedSize(30, 30)
            b.setIcon(_accent_pixmap(color))
            b.setIconSize(_accent_pixmap(color).size())
            b.setProperty("swatch", True)
            b.setToolTip(name.title())
            b.clicked.connect(lambda _c, n=name: self._pick_accent(n))
            accent_lay.addWidget(b)
            self._accent_buttons[name] = b
        accent_lay.addStretch(1)
        appearance.add_row("Accent color", accent_widget)

        # ---- General
        general = Section("General")
        self.root_edit = QLineEdit(projects_root)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_root)
        root_widget = QWidget()
        rw = QHBoxLayout(root_widget)
        rw.setContentsMargins(0, 0, 0, 0)
        rw.addWidget(self.root_edit)
        rw.addWidget(browse)
        self.root_edit.setMinimumWidth(260)
        general.add_row("Projects folder", root_widget)
        self.minimized_check = QCheckBox("Start minimized to the tray")
        self.minimized_check.setChecked(start_minimized)
        general.add_row("Startup", self.minimized_check)
        self.tray_check = QCheckBox("Keep running in the tray when closed")
        general.add_row("On close", self.tray_check,
                        "Quit from the tray menu when you really mean it")

        # ---- Network defaults
        network = Section("Network defaults")
        self.conc_spin = QSpinBox()
        self.conc_spin.setRange(1, 32)
        self.conc_spin.setValue(8)
        network.add_row("Default concurrency", self.conc_spin)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 2000)
        self.delay_spin.setSingleStep(50)
        self.delay_spin.setValue(200)
        self.delay_spin.setSuffix(" ms")
        network.add_row("Default per-host delay", self.delay_spin)

        # ---- Shortcuts
        shortcuts = Section("Keyboard shortcuts")
        for keys, what in SHORTCUTS:
            k = QLabel(keys)
            k.setProperty("cardtitle", True)
            shortcuts.add_row(what, k)

        lay.addWidget(appearance)
        lay.addWidget(general)
        lay.addWidget(network)
        lay.addWidget(shortcuts)
        lay.addStretch(1)
        self._sync_visual_state()

    # ------------------------------------------------------------- handlers

    def _pick_theme(self, name: str):
        self._theme = name
        self.theme_changed.emit(name)
        self._sync_visual_state()

    def _pick_accent(self, name: str):
        self._accent = name
        self.accent_changed.emit(name)
        self._sync_visual_state()

    def _sync_visual_state(self):
        # selection via dynamic property + repolish — never per-widget
        # stylesheets, which would fight the theme engine
        for name, b in self._theme_buttons.items():
            if (name == self._theme) != (b.property("selected") is True):
                b.setProperty("selected", name == self._theme)
                b.style().unpolish(b)
                b.style().polish(b)
        for name, b in self._accent_buttons.items():
            if (name == self._accent) != (b.property("selected") is True):
                b.setProperty("selected", name == self._accent)
                b.style().unpolish(b)
                b.style().polish(b)

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, "Choose the projects folder")
        if d:
            self.root_edit.setText(d)

    def current_general(self) -> dict:
        return {"projects_root": self.root_edit.text().strip(),
                "start_minimized": self.minimized_check.isChecked(),
                "close_to_tray": self.tray_check.isChecked()}

    def current_theme(self) -> str:
        return self._theme

    def current_accent(self) -> str:
        return self._accent
