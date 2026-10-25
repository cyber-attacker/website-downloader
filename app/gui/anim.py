"""One place for all motion (spec §3/§11): a single animate() helper, view
cross-fades, button press-scale, count-up numbers. Reduced-motion collapses
everything to <50ms. Only opacity/geometry properties are animated — never
layout-heavy ones.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect

from app.gui import theme_engine

DUR = dict(fast=120, normal=180, slow=250, enter=180)


def _dur(ms: int) -> int:
    if theme_engine.reduced_motion():
        return 40 if ms > 40 else ms
    return ms


def animate(widget, prop, start, end, duration: int = DUR["normal"],
            curve=QEasingCurve.OutCubic, on_done=None):
    """QPropertyAnimation with sane defaults; returns the animation."""
    prop = prop.encode() if isinstance(prop, str) else prop
    anim = QPropertyAnimation(widget, prop, widget)
    anim.setDuration(_dur(duration))
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(curve if not theme_engine.reduced_motion()
                        else QEasingCurve.Linear)
    if on_done:
        anim.finished.connect(on_done)
    anim.start(QPropertyAnimation.DeleteWhenStopped)
    return anim


def fade_in(widget, duration: int = DUR["enter"], slide_up: int = 0):
    """0→1 opacity (+optional N-px upward settle) for entering cards/views."""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    eff.setOpacity(0.0)
    if slide_up:
        y_start = widget.y() + slide_up
        widget.move(widget.x(), y_start)
        var = QVariantAnimation(widget)
        var.setDuration(_dur(duration))
        var.setStartValue(float(y_start))
        var.setEndValue(float(widget.y() - slide_up))

        def _set_y(val):
            widget.move(widget.x(), int(val))

        var.valueChanged.connect(_set_y)
        var.start(QVariantAnimation.DeleteWhenStopped)
    return animate(eff, b"opacity", 0.0, 1.0, duration,
                   on_done=lambda: widget.setGraphicsEffect(None))


def cross_fade(stack, index: int, duration: int = DUR["normal"]):
    """Switch a QStackedWidget with a 0→1 opacity fade on the incoming page."""
    if stack.currentIndex() == index:
        return
    stack.setCurrentIndex(index)
    page = stack.widget(index)
    eff = QGraphicsOpacityEffect(page)
    page.setGraphicsEffect(eff)
    eff.setOpacity(0.0)
    animate(eff, b"opacity", 0.0, 1.0, duration,
            on_done=lambda: page.setGraphicsEffect(None))


class PressScale:
    """Event-filter that scales a widget to 97% while pressed (spec §3).

    attach:  filt = PressScale(button); button.installEventFilter(filt)
    """

    def __init__(self, widget) -> None:
        self._w = widget
        self._anim = None

    def _scale_to(self, factor: float):
        if theme_engine.reduced_motion():
            return
        g = self._w.geometry()
        w2, h2 = int(g.width() * factor), int(g.height() * factor)
        target = g.adjusted((g.width() - w2) // 2, (g.height() - h2) // 2,
                            -((g.width() - w2 + 1) // 2), -((g.height() - h2 + 1) // 2))
        self._anim = animate(self._w, b"geometry", g, target, DUR["fast"],
                             QEasingCurve.OutCubic)

    def eventFilter(self, obj, event):           # noqa: N802 — Qt naming
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.KeyPress and event.isAutoRepeat():
            return False
        if event.type() in (QEvent.MouseButtonPress, QEvent.KeyPress):
            self._scale_to(0.97)
        elif event.type() in (QEvent.MouseButtonRelease, QEvent.KeyRelease):
            self._scale_to(1.0)
        return False


def attach_press_scale(widget) -> PressScale:
    filt = PressScale(widget)
    widget.installEventFilter(filt)
    return filt


def count_up(label, from_value: int, to_value: int,
             duration: int = 380, fmt="{:,}"):
    """Animate a number in a label (spec §8 stat cards)."""
    if theme_engine.reduced_motion() or from_value == to_value:
        label.setText(fmt.format(to_value))
        return None
    var = QVariantAnimation(label)
    var.setDuration(duration)
    var.setStartValue(from_value)
    var.setEndValue(to_value)
    var.setEasingCurve(QEasingCurve.OutCubic)

    def _set(val):
        label.setText(fmt.format(int(val)))

    var.valueChanged.connect(_set)
    var.start(QVariantAnimation.DeleteWhenStopped)
    return var


def flash_background(widget, msec: int = 600):
    """Brief highlight pulse when a card's status changes live (spec §3):
    a fast opacity dip that springs back."""
    eff = widget.graphicsEffect()
    if not isinstance(eff, QGraphicsOpacityEffect):
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
    if theme_engine.reduced_motion():
        return
    animate(eff, b"opacity", 1.0, 0.55, msec // 3,
            on_done=lambda: animate(eff, b"opacity", 0.55, 1.0, msec))
