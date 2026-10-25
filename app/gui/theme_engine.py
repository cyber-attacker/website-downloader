"""Theme engine: token dicts -> generated QSS. No hex value ever lives inside a
widget file (spec §11). Five themes (light/dark/midnight/high-contrast/system)
+ a runtime accent override; switching is instant and persisted by the caller.

Reduced-motion detection lives here too — one place, queried by app.gui.anim.
"""
from __future__ import annotations

import ctypes
from typing import Any

# ---------------------------------------------------------------- tokens

def _t(bg, surface, surface2, border, text, text2, accent, success, warning,
       danger, radius=8, card_radius=12, border_w="1px", focus_ring=True):
    return {
        "bg": bg, "surface": surface, "surface2": surface2, "border": border,
        "text": text, "text2": text2, "accent": accent, "on_accent": "#FFFFFF",
        "success": success, "warning": warning, "danger": danger,
        "radius": f"{radius}px", "card_radius": f"{card_radius}px",
        "border_w": border_w, "focus_ring": focus_ring,
        "shadow": "rgba(0,0,0,0.10)" if bg.lstrip("#")[0] in "F" else "rgba(0,0,0,0.35)",
        # derived per-status tints are computed at QSS build time via _mix
    }


THEMES: dict[str, dict[str, Any]] = {
    "light": _t(
        bg="#F5F5F7", surface="#FFFFFF", surface2="#EDEDF0", border="#E2E2E6",
        text="#17171A", text2="#6B6B74",
        accent="#2F6FED", success="#1E9E64", warning="#D97706", danger="#DC3545"),
    "dark": _t(
        bg="#1A1A1E", surface="#232327", surface2="#2B2B31", border="#323238",
        text="#F0F0F2", text2="#9A9AA2",
        accent="#4F83F7", success="#2FBF71", warning="#F59E0B", danger="#F05462"),
    "midnight": _t(
        bg="#0C0C0E", surface="#17171A", surface2="#1F1F24", border="#26262B",
        text="#F2F2F4", text2="#8E8E98",
        accent="#5B8CFF", success="#2CCB7A", warning="#F5A623", danger="#FF5A67"),
    "high_contrast": _t(
        bg="#FFFFFF", surface="#FFFFFF", surface2="#F0F0F0", border="#000000",
        text="#000000", text2="#1A1A1A",
        accent="#0000EE", success="#00600B", warning="#7A4A00", danger="#C00000",
        radius=6, card_radius=6, border_w="1.5px"),
}

# swatch accents pickable in Settings, applied on top of any theme
ACCENTS: dict[str, str] = {
    "blue": "#2F6FED", "teal": "#0E9888", "violet": "#8B5CF6",
    "coral": "#EF5B4C", "green": "#1E9E64",
}

_active_theme = "system"
_active_accent = "blue"


# ---------------------------------------------------------------- helpers

def _hex_mix(a: str, b: str, t: float) -> str:
    """Blend hex colors; t=0 -> a, t=1 -> b."""
    def parts(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    pa, pb = parts(a), parts(b)
    return "#%02X%02X%02X" % tuple(round(x + (y - x) * t) for x, y in zip(pa, pb))


def _alpha(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"#{h}{int(round(alpha * 255)):02X}"


def system_prefers_dark() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return value == 0
    except OSError:
        return False


def reduced_motion() -> bool:
    """Windows 'animate controls' accessibility flag (spec §3)."""
    try:
        param = ctypes.c_uint(0)
        SPI_GETCLIENTAREAANIMATION = 0x1042
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(param), 0)
        return ok and param.value == 0
    except Exception:                        # noqa: BLE001 — non-Windows safe
        return False


def resolve(theme: str) -> str:
    """'system' resolves to the light/dark theme the OS currently prefers."""
    if theme == "system":
        return "dark" if system_prefers_dark() else "light"
    return theme


def tokens(theme: str = None, accent: str = None) -> dict[str, Any]:
    """Effective tokens for the active (theme, accent) pair, with all the
    derived tints a screen might need."""
    name = resolve(theme or _active_theme)
    base = dict(THEMES.get(name, THEMES["light"]))
    if accent and accent in ACCENTS:
        base["accent"] = ACCENTS[accent]
    elif accent and accent.startswith("#"):
        base["accent"] = accent
    bg, surface = base["bg"], base["surface"]
    base.update({
        "accent_hover": _hex_mix(base["accent"], "#000000", 0.12),
        "accent_pressed": _hex_mix(base["accent"], "#000000", 0.22),
        "accent_tint": _alpha(base["accent"], 0.13),
        "accent_tint_text": _hex_mix(base["accent"], base["text"], 0.25),
        "success_tint": _alpha(base["success"], 0.14),
        "success_text": _hex_mix(base["success"], base["text"], 0.18),
        "warning_tint": _alpha(base["warning"], 0.16),
        "warning_text": _hex_mix(base["warning"], base["text"], 0.15),
        "danger_tint": _alpha(base["danger"], 0.14),
        "danger_text": _hex_mix(base["danger"], base["text"], 0.15),
        "hover": _alpha("#808080", 0.10),
        "sidebar_bg": _hex_mix(bg, "#000000", 0.05) if name != "high_contrast" else bg,
        "shadow": "rgba(0,0,0,0.06)" if name == "light" else "rgba(0,0,0,0.55)",
    })
    return base


def set_active(theme: str, accent: str | None = None) -> None:
    global _active_theme, _active_accent
    _active_theme = theme
    if accent:
        _active_accent = accent


def active() -> tuple[str, str]:
    return _active_theme, _active_accent


# ---------------------------------------------------------------- QSS build

def build_qss(theme: str, accent: str | None = None) -> str:
    """Generate the full stylesheet from tokens (spec §2/§11)."""
    t = tokens(theme, accent)
    ring = (f"border: {t['border_w']} solid {t['accent']};" if t["focus_ring"]
            else f"border: {t['border_w']} solid {t['text']};")
    return f"""
* {{ font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif;
     font-size: 13px; color: {t['text']}; }}
QWidget {{ background: {t['bg']}; }}
QToolTip {{ background: {t['surface']}; color: {t['text']};
             border: {t['border_w']} solid {t['border']}; border-radius: 6px; }}

/* ---------- frames & cards ---------- */
QFrame#Card, QFrame[card="true"] {{
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['card_radius']};
}}
QFrame#TitleBar {{ background: {t['bg']}; border: none; }}
QFrame#Sidebar {{ background: {t['sidebar_bg']}; border: none; }}
QLabel[muted="true"] {{ color: {t['text2']}; background: transparent; }}
QLabel[heading="true"] {{ font-size: 21px; font-weight: 600; background: transparent; }}
QLabel[cardtitle="true"] {{ font-size: 14px; font-weight: 600; background: transparent; }}
QFrame#Divider {{ background: {t['border']}; border: none; max-height: 1px; }}

/* ---------- buttons ---------- */
QPushButton {{
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['radius']}; padding: 6px 14px; min-height: 20px;
}}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['surface2']}; }}
QPushButton:focus {{ {ring.replace('border:', 'outline: none; border:')} }}
QPushButton[variant="primary"] {{
    background: {t['accent']}; color: {t['on_accent']}; border: none; font-weight: 600;
}}
QPushButton[variant="primary"]:hover {{ background: {t['accent_hover']}; }}
QPushButton[variant="primary"]:pressed {{ background: {t['accent_pressed']}; }}
QPushButton[variant="danger"]:hover {{ background: {t['danger_tint']};
                                       color: {t['danger_text']}; border-color: {t['danger']}; }}
QPushButton[iconbtn="true"] {{
    background: transparent; border: {t['border_w']} solid transparent;
    border-radius: 8px; min-width: 30px; min-height: 30px; padding: 3px;
}}
QPushButton[iconbtn="true"]:hover {{ background: {t['hover']};
                                     border-color: {t['border']}; }}
QPushButton[variant="primary"][disabled="true"], QPushButton:disabled {{
    color: {t['text2']}; background: {t['surface2']};
    border-color: {t['border']};
}}

/* ---------- nav buttons (sidebar) ---------- */
QToolButton[nav="true"] {{
    background: transparent; border: none; border-radius: 9px;
    padding: 9px 12px; text-align: left; font-size: 13px; color: {t['text2']};
}}
QToolButton[nav="true"]:hover {{ background: {t['hover']}; color: {t['text']}; }}
QToolButton[nav="true"]:checked {{
    background: {t['accent_tint']}; color: {t['accent_tint_text']}; font-weight: 600;
}}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['radius']}; padding: 6px 10px; selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus {{ border-color: {t['accent']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    background: {t['surface2']}; color: {t['text2']};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['radius']}; selection-background-color: {t['accent_tint']};
    selection-color: {t['accent_tint_text']}; outline: none;
}}

/* ---------- progress ---------- */
QProgressBar {{
    background: {t['surface2']}; border: none; border-radius: 3px;
    min-height: 6px; max-height: 6px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
QProgressBar[status="completed"]::chunk {{ background: {t['success']}; }}
QProgressBar[status="failed"]::chunk {{ background: {t['danger']}; }}
QProgressBar[thincount="true"] {{ max-height: 3px; min-height: 3px; }}

/* ---------- status pills ---------- */
QLabel[pill="running"]   {{ background: {t['accent_tint']};   color: {t['accent_tint_text']};
                             border-radius: 10px; padding: 3px 10px; font-weight: 600; }}
QLabel[pill="completed"] {{ background: {t['success_tint']};  color: {t['success_text']};
                             border-radius: 10px; padding: 3px 10px; font-weight: 600; }}
QLabel[pill="paused"]    {{ background: {t['warning_tint']};  color: {t['warning_text']};
                             border-radius: 10px; padding: 3px 10px; font-weight: 600; }}
QLabel[pill="failed"]    {{ background: {t['danger_tint']};   color: {t['danger_text']};
                             border-radius: 10px; padding: 3px 10px; font-weight: 600; }}
QLabel[pill="muted"]     {{ background: {t['surface2']};      color: {t['text2']};
                             border-radius: 10px; padding: 3px 10px; font-weight: 600; }}

/* pulse animation for the running pill (toggled by the dashboard) */
QLabel[pill="running"][pulse="true"] {{ background: {t['accent']}; color: {t['on_accent']}; }}

/* ---------- lists / trees / logs ---------- */
QListWidget, QTreeWidget, QTreeView {{
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['card_radius']}; outline: none; padding: 4px;
}}
QListWidget::item {{ border-radius: 6px; padding: 6px 8px; }}
QListWidget::item:hover {{ background: {t['hover']}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {t['accent_tint']}; color: {t['accent_tint_text']};
}}
QTreeWidget::item:hover {{ background: {t['hover']}; }}
QHeaderView::section {{
    background: {t['surface']}; color: {t['text2']}; border: none;
    border-bottom: {t['border_w']} solid {t['border']}; padding: 6px 8px;
}}
QPlainTextEdit#LogView {{
    font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
    background: {t['surface']}; border: {t['border_w']} solid {t['border']};
    border-radius: {t['card_radius']};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['border']}; border-radius: 4px;
                               min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['text2']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['border']}; border-radius: 4px;
                                 min-width: 30px; }}

/* ---------- sliders / checkboxes ---------- */
QSlider::groove:horizontal {{ background: {t['surface2']}; height: 6px;
                              border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {t['surface']}; border: 2px solid {t['accent']};
                              width: 16px; height: 16px; margin: -6px 0;
                              border-radius: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 17px; height: 17px; border-radius: 5px;
    border: {t['border_w']} solid {t['border']}; background: {t['surface']};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {t['accent']}; border-color: {t['accent']};
    image: url(none);
}}

/* ---------- chips ---------- */
QToolButton[chip="true"] {{
    background: transparent; border: {t['border_w']} solid {t['border']};
    border-radius: 12px; padding: 3px 12px; color: {t['text2']};
}}
QToolButton[chip="true"]:hover {{ background: {t['hover']}; }}
QToolButton[chip="true"]:checked {{
    background: {t['accent_tint']}; color: {t['accent_tint_text']};
    border-color: {t['accent']}; font-weight: 600;
}}

/* ---------- toasts ---------- */
QFrame#Toast {{ background: {t['surface']}; border: {t['border_w']} solid {t['border']};
                 border-radius: 10px; }}
QLabel#ToastCountdown {{ background: {t['accent']}; border: none; max-height: 3px; }}

/* ---------- dialogs ---------- */
QDialog {{ background: {t['bg']}; }}
QMessageBox {{ background: {t['surface']}; }}
QStatusBar {{ background: {t['bg']}; border-top: {t['border_w']} solid {t['border']};
               color: {t['text2']}; }}
QMenu {{ background: {t['surface']}; border: {t['border_w']} solid {t['border']};
         border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {t['accent_tint']}; color: {t['accent_tint_text']}; }}
"""


def apply(app, theme: str, accent: str | None = None) -> dict:
    """Instant, app-wide theme switch. Returns the effective tokens."""
    set_active(theme, accent)
    app.setStyleSheet(build_qss(theme, accent))
    return tokens(theme, accent)


# keep the old entry point working (imports elsewhere reference theme.py)
def apply_theme(app, theme: str) -> None:
    if theme == "system":
        app.setStyleSheet(build_qss("system"))
    else:
        apply(app, theme)
