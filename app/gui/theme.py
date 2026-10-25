"""Theming: dark/light/system with qt-material, graceful fallback to a small
built-in QSS so the app still looks right if the optional dep is missing."""
from __future__ import annotations

DARK_QSS = """
QWidget { background-color: #1e1f22; color: #dfe1e5; font-size: 13px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QListWidget, QTableWidget {
    background-color: #2b2d30; border: 1px solid #3c3f44; border-radius: 4px;
    selection-background-color: #37563f;
}
QPushButton {
    background-color: #393b40; border: 1px solid #4c5055; padding: 5px 14px;
    border-radius: 4px;
}
QPushButton:hover { background-color: #454a52; }
QPushButton:disabled { color: #7a7f87; }
QGroupBox { border: 1px solid #3c3f44; border-radius: 6px; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QProgressBar { border: 1px solid #3c3f44; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background-color: #4f9cf9; }
QMenuBar { background-color: #2b2d30; }
QMenu { background-color: #2b2d30; border: 1px solid #3c3f44; }
QMenu::item:selected { background-color: #37563f; }
QTableView::item:selected, QTableWidget::item:selected { background-color: #37563f; }
"""

LIGHT_QSS = """
QWidget { font-size: 13px; }
QGroupBox { border: 1px solid #c8ccd0; border-radius: 6px; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
"""


def apply_theme(app, theme: str) -> None:
    """theme: 'dark' | 'light' | 'system'. qt-material when available,
    built-in QSS otherwise; 'system' clears everything back to the default."""
    theme = (theme or "system").lower()
    if theme == "system":
        app.setStyleSheet("")
        return
    try:
        from qt_material import apply_stylesheet

        apply_stylesheet(app, theme="dark_teal.xml" if theme == "dark" else "light.xml")
        return
    except Exception:                     # noqa: BLE001 — optional dependency
        pass
    app.setStyleSheet(DARK_QSS if theme == "dark" else LIGHT_QSS)
