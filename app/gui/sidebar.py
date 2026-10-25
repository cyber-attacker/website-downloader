"""Persistent left sidebar (spec §4): nav rows with icons, animated collapse
to icon-only width, muted version text at the bottom."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.gui import anim, widgets

EXPANDED_W = 232
COLLAPSED_W = 56


class Sidebar(QFrame):
    navigate = Signal(int)          # 0 projects, 1 new project, 2 settings
    collapse_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(EXPANDED_W)
        self._collapsed = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 14, 12, 12)
        lay.setSpacing(4)

        # logo row: accent download glyph + wordmark
        logo_row = QHBoxLayout()
        from PySide6.QtGui import QPixmap, QColor, QPainter, QBrush, QPen, QFont
        logo_row.addSpacing(4)

        glyph = QLabel()
        glyph.setPixmap(widgets.accent_icon("download", 18).pixmap(20, 20))
        logo_row.addWidget(glyph)
        self.wordmark = QLabel("Website\nDownloader")
        self.wordmark.setProperty("cardtitle", True)
        logo_row.addSpacing(8)
        logo_row.addWidget(self.wordmark)
        logo_row.addStretch(1)
        lay.addLayout(logo_row)
        lay.addSpacing(14)

        self._nav_buttons: list[QToolButton] = []
        self._nav_meta: list[tuple[str, str]] = [
            ("folder", "Projects"),
            ("plus-circle", "New Project"),
            ("cog", "Settings"),
        ]
        for i, (icon_name, text) in enumerate(self._nav_meta):
            b = QToolButton()
            b.setProperty("nav", True)
            b.setCheckable(True)
            b.setIcon(widgets.icon(icon_name))
            b.setText("  " + text)
            b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c, idx=i: self._emit(idx))
            lay.addWidget(b)
            self._nav_buttons.append(b)
        self._nav_buttons[0].setChecked(True)

        lay.addStretch(1)

        # collapse toggle pinned at the bottom
        self._collapse_btn = QToolButton()
        self._collapse_btn.setProperty("nav", True)
        self._collapse_btn.setIcon(widgets.icon("chevron-left"))
        self._collapse_btn.setText("")
        self._collapse_btn.clicked.connect(self.toggle_collapse)
        lay.addWidget(self._collapse_btn)

        self.version = QLabel(f"v{__version__}")
        self.version.setProperty("muted", True)
        lay.addWidget(self.version, 0, Qt.AlignLeft)

    # ---- nav ------------------------------------------------------------

    def _emit(self, idx: int):
        for i, b in enumerate(self._nav_buttons):
            b.setChecked(i == idx)
        self.navigate.emit(idx)

    def set_active(self, idx: int):
        for i, b in enumerate(self._nav_buttons):
            b.setChecked(i == idx)

    # ---- collapse ----------------------------------------------------------

    def toggle_collapse(self, collapsed: bool | None = None):
        self._collapsed = (not self._collapsed) if collapsed is None else collapsed
        target = COLLAPSED_W if self._collapsed else EXPANDED_W
        anim.animate(self, b"minimumWidth", self.width(), target, 220,
                     QEasingCurve.OutCubic)
        anim.animate(self, b"maximumWidth", self.width(), target, 220,
                     QEasingCurve.OutCubic)
        self.wordmark.setVisible(not self._collapsed)
        self.version.setVisible(not self._collapsed)
        for b, (icon_name, text) in zip(self._nav_buttons, self._nav_meta):
            b.setIcon(widgets.icon(icon_name))
            if self._collapsed:
                b.setText("")
                b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            else:
                b.setText("  " + text)
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._collapse_btn.setIcon(
            widgets.icon("chevron-right" if self._collapsed else "chevron-left"))
        self.collapse_toggled.emit(self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed
