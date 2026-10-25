"""Shared UI components (spec §10): every widget styled via the theme
engine's QSS — never default Qt gray. Icons come from qtawesome (Feather set,
consistent stroke weight)."""
from __future__ import annotations

import qtawesome as qta
from PySide6.QtCore import QEasingCurve, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.gui import anim, theme_engine

# feather-style icons through qtawesome — one consistent set app-wide (spec §4)
def icon(name: str, color: str | None = None, size: int = 16):
    tok = theme_engine.tokens()
    prefix = name if "." in name else "fa5s." + name
    try:
        return qta.icon(prefix, color=color or tok["text2"],
                        color_disabled=tok["border"])
    except Exception:                       # noqa: BLE001 — a bad name must
        return qta.icon("fa5s.circle",   # never crash the UI
                        color=color or tok["text2"])


def accent_icon(name: str, size: int = 16):
    tok = theme_engine.tokens()
    return icon(name, tok["accent"], size)


def icon_btn(name: str, tooltip: str = "", size: int = 30) -> QPushButton:
    b = QPushButton()
    b.setIcon(icon(name))
    b.setFixedSize(size, size)
    b.setProperty("iconbtn", True)
    if tooltip:
        b.setToolTip(tooltip)          # delayed by Qt, styled via QSS (spec §5)
    return b


class Card(QFrame):
    """Base card: surface background, border, 12px radius (spec §10)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("card", True)


class StatusPill(QLabel):
    """Rounded pill colored by state (running/completed/paused/failed/muted)."""

    def __init__(self, text: str = "", state: str = "muted", parent=None):
        super().__init__(text, parent)
        self.set_state(state)

    def set_state(self, state: str, text: str | None = None):
        self.setProperty("pill", state)
        if text is not None:
            self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)


class AnimatedProgressBar(QProgressBar):
    """Thin (6px) rounded bar; width changes animate instead of snapping
    (spec §10). Instant `set_value_now` for programmatic/test use."""

    def __init__(self, status: str = "", parent=None):
        super().__init__(parent)
        self.setTextVisible(False)
        self.setRange(0, 100)
        if status:
            self.setProperty("status", status)
        self._anim = None

    def animate_to(self, value: int, duration: int = 260):
        self.setProperty("status", "completed" if value >= 100 else self.property("status"))
        if theme_engine.reduced_motion():
            self.setValue(value)
            return
        if self._anim is not None:
            self._anim.stop()
        self._anim = anim.animate(self, b"value", self.value(), max(0, min(100, value)),
                                  duration, QEasingCurve.OutCubic)

    def set_value_now(self, value: int):
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self.setValue(value)

    def celebrate(self):               # quick fill-to-100 pulse (spec §3)
        self.animate_to(100, 420)


class StatCard(QFrame):
    """One dashboard stat: muted caption + big number that count-ups."""

    def __init__(self, caption: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setProperty("card", True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(2)
        top = QHBoxLayout()
        cap = QLabel(caption)
        cap.setProperty("muted", True)
        top.addWidget(cap)
        top.addStretch(1)
        ic = QLabel()
        ic.setPixmap(accent_icon(icon_name).pixmap(QSize(16, 16)))
        top.addWidget(ic)
        lay.addLayout(top)
        self.number = QLabel("0")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        self.number.setFont(f)
        lay.addWidget(self.number)
        self._last = 0

    def set_value(self, v: int, fmt="{:,}"):
        anim.count_up(self.number, self._last, v, fmt=fmt)
        self._last = v


class EmptyState(QFrame):
    """Designed empty state (spec §5): icon + heading + subtitle + button."""

    def __init__(self, icon_name: str, title: str, subtitle: str,
                 button_text: str = "", on_click=None, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(48, 48, 48, 48)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignCenter)
        ic = QLabel()
        ic.setPixmap(icon(icon_name, theme_engine.tokens()["text2"], 36)
                     .pixmap(QSize(44, 44)))
        lay.addWidget(ic, 0, Qt.AlignCenter)
        t = QLabel(title)
        f = QFont(); f.setPointSize(15); f.setBold(True)
        t.setFont(f)
        lay.addWidget(t, 0, Qt.AlignCenter)
        s = QLabel(subtitle)
        s.setProperty("muted", True)
        s.setWordWrap(True)
        lay.addWidget(s, 0, Qt.AlignCenter)
        if button_text and on_click:
            btn = QPushButton(button_text)
            btn.setProperty("variant", "primary")
            btn.clicked.connect(on_click)
            lay.addSpacing(12)
            lay.addWidget(btn, 0, Qt.AlignHCenter)


class Sparkline(QWidget):
    """Rolling line chart of the last N samples (speed history on the live
    dashboard). Painted with the active accent — theme-correct for free."""

    def __init__(self, samples: int = 90, parent=None):
        super().__init__(parent)
        self._samples: list[float] = []
        self._max_samples = samples
        self.setMinimumHeight(36)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._peak = 0.0

    def push(self, value: float) -> None:
        self._samples.append(max(0.0, float(value)))
        if len(self._samples) > self._max_samples:
            self._samples.pop(0)
        self._peak = max(self._samples) if self._samples else 0.0
        self.update()

    def clear(self) -> None:
        self._samples.clear()
        self._peak = 0.0
        self.update()

    def paintEvent(self, event):              # noqa: N802 — Qt naming
        if len(self._samples) < 2:
            return
        tok = theme_engine.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        span = self._peak or 1.0
        pts = []
        for i, v in enumerate(self._samples):
            x = i * (w - 2) / (len(self._samples) - 1) + 1
            y = h - 3 - (v / span) * (h - 8)
            pts.append(QPoint(int(x), int(y)))
        # fill under the line
        from PySide6.QtGui import QPolygon, QColor, QBrush
        poly = QPolygon(pts + [pts[-1], pts[0]] + [QPoint(pts[0].x(), h - 2),
                                                   QPoint(pts[-1].x(), h - 2)])
        fill = QColor(tok["accent"])
        fill.setAlpha(40)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(fill))
        p.drawPolygon(poly)
        # line
        pen = QPen(QColor(tok["accent"]))
        pen.setWidthF(1.6)
        p.setPen(pen)
        p.drawPolyline(QPolygon(pts))
        # peak caption
        p.setPen(QColor(tok["text2"]))
        p.drawText(4, 12, f"peak {span:.1f}")
        p.end()


class SkeletonRows(QWidget):
    """Shimmer skeleton while a list first loads (spec §3/§5): soft gradient
    sweep left-to-right, no spinner, no blank screen."""

    def __init__(self, rows: int = 5, parent=None):
        super().__init__(parent)
        self._rows = rows
        self._phase = 0.0
        self.setFixedHeight(rows * 72)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def _tick(self):
        self._phase = (self._phase + 0.016) % 1.0
        self.update()

    def paintEvent(self, event):              # noqa: N802 — Qt naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tok = theme_engine.tokens()
        base = QColor(tok["surface2"])
        hi = QColor(tok["border"])
        p.setPen(Qt.NoPen)
        w = self.width()
        for i in range(self._rows):
            y = 14 + i * 72
            r = QRect(8, y, w - 16, 58)
            p.setBrush(base)
            p.drawRoundedRect(r, 12, 12)
            # shimmer sweep
            sweep_x = int((self._phase * 1.6 - 0.3) * w)
            p.setClipRect(r)
            pen = QPen(hi, 26)
            pen.setColor(QColor(hi.red(), hi.green(), hi.blue(), 90))
            p.setPen(pen)
            p.drawLine(sweep_x, y, sweep_x + 90, y + 58)
            p.setClipping(False)
        p.end()

    def stop(self):
        self._timer.stop()


class SegmentedControl(QFrame):
    """Compact segmented toggle (used for scope choices, log filters)."""

    changed = Signal(int)

    def __init__(self, options: list[str], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._buttons = []
        for i, text in enumerate(options):
            b = QPushButton(text)
            b.setProperty("chip", True)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.clicked.connect(lambda _c, idx=i: self._pick(idx))
            lay.addWidget(b)
            self._buttons.append(b)

    def _pick(self, idx: int):
        for i, b in enumerate(self._buttons):
            b.setChecked(i == idx)
        self.changed.emit(idx)

    def set_current(self, idx: int):
        self._pick(idx)

    def current(self) -> int:
        for i, b in enumerate(self._buttons):
            if b.isChecked():
                return i
        return 0


def _make_check(text: str, checked: bool = False) -> "QCheckBox":
    from PySide6.QtWidgets import QCheckBox
    cb = QCheckBox(text)
    cb.setChecked(checked)
    return cb


# ---------------------------------------------------------------- toasts

class Toast(QFrame):
    """Non-blocking notification (spec §5): icon + message, slide/fade in,
    auto-dismiss with a thin countdown bar."""

    def __init__(self, message: str, severity: str = "info", msec: int = 4000):
        super().__init__()
        self.setObjectName("Toast")
        self.setProperty("card", True)
        self.setFixedHeight(44)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        tok = theme_engine.tokens()
        color = {"info": tok["accent"], "success": tok["success"],
                 "warning": tok["warning"], "error": tok["danger"]}[severity]
        glyph = {"info": "info-circle", "success": "check-circle",
                 "warning": "exclamation-triangle", "error": "times-circle"}[severity]
        ic = QLabel()
        ic.setPixmap(icon(glyph, color).pixmap(QSize(16, 16)))
        lay.addWidget(ic)
        txt = QLabel(message)
        txt.setWordWrap(True)
        lay.addWidget(txt, stretch=1)

        # countdown strip pinned along the bottom edge
        strip = QProgressBar(self)
        strip.setProperty("thincount", True)
        strip.setTextVisible(False)
        strip.setRange(0, msec)
        strip.setValue(msec)
        strip.setGeometry(0, self.height() - 3, 1, 3)
        self._strip = strip
        strip.raise_()

        self._bar_timer = QTimer(self)
        self._bar_timer.timeout.connect(self._tick_bar)
        self._bar_timer.start(50)
        self._remaining = msec
        QTimer.singleShot(msec, self.dismiss)

    def _tick_bar(self):
        self._remaining -= 50
        self._strip.setValue(max(0, self._remaining))
        self._strip.setGeometry(4, self.height() - 6, self.width() - 8, 3)

    def showEvent(self, event):                    # noqa: N802 — Qt naming
        super().showEvent(event)
        anim.fade_in(self, anim.DUR["normal"])

    def dismiss(self):
        self._bar_timer.stop()
        eff = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(eff)
        anim.animate(eff, b"opacity", 1.0, 0.0, 200,
                     on_done=self.deleteLater)


class ToastHost(QWidget):
    """Overlay in the top-right corner stacking toasts — bounded to a small
    strip so it can never sit over content, and click-transparent anyway."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignTop | Qt.AlignRight)
        parent.installEventFilter(self)

    def eventFilter(self, obj, event):            # noqa: N802 — Qt naming
        from PySide6.QtCore import QEvent
        if obj is self.parent() and event.type() in (QEvent.Resize, QEvent.Show):
            self.setGeometry(self.parent().width() - 356, 8, 348, 300)
        return False

    def toast(self, message: str, severity: str = "info", msec: int = 4000):
        t = Toast(message, severity, msec)
        self.layout().addWidget(t, 0, Qt.AlignTop | Qt.AlignRight)
        return t
