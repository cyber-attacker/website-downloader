"""Entry point: `python -m app.gui` (or the packaged WebsiteDownloader.exe,
which forwards --cli arguments to the CLI for scripted crawls)."""
import os
import sys


def main() -> int:
    if "--cli" in sys.argv:
        # packaged windowed builds have no console; still allow scripted runs
        from app.cli import main as cli_main

        return cli_main([a for a in sys.argv[1:] if a != "--cli"])

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication, QSplashScreen

    from app import __version__
    from app.gui import theme_engine

    QApplication.setAttribute(Qt.AA_DontShowIconsInMenus, False)
    app = QApplication(sys.argv)
    app.setApplicationName("Website Downloader")
    app.setApplicationVersion(__version__)

    # spec §1 font stack: Inter > Segoe UI Variable > Segoe UI > system
    fams = set(QFontDatabase.families())
    for candidate in ("Inter", "Segoe UI Variable Text", "Segoe UI Variable",
                      "Segoe UI"):
        if candidate in fams:
            app.setFont(QFont(candidate, 10))
            break

    # theme-aware branded splash while the window constructs (spec §5)
    from PySide6.QtGui import QColor, QPainter, QPixmap
    tok = theme_engine.tokens()
    pm = QPixmap(420, 220)
    p = QPainter(pm)
    p.fillRect(pm.rect(), QColor(tok["bg"]))
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(tok["accent"]))
    p.drawRoundedRect(24, 56, 56, 56, 14, 14)
    p.setPen(QColor(tok["text"]))
    f = QFont(app.font()); f.setPointSizeF(20); f.setBold(True)
    p.setFont(f)
    p.drawText(100, 92, "Website Downloader")
    f2 = QFont(app.font()); f2.setPointSizeF(10)
    p.setFont(f2)
    p.setPen(QColor(tok["text2"]))
    p.drawText(100, 118, f"v{__version__} — starting…")
    p.end()
    splash = QSplashScreen(pm)
    splash.show()
    app.processEvents()

    from app.gui.main_window import MainWindow

    window = MainWindow()
    window.show()
    splash.finish(window)
    return app.exec()


def run() -> int:
    if getattr(sys, "frozen", False):
        # frozen windowed builds: std streams can be fully detached, and a
        # bare NULL handle blocks at the C level on write. Give Python real
        # NUL-device streams up front so print/flush always work, detached
        # or not (also makes --cli usable from scheduled tasks).
        for name in ("stdout", "stderr"):
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    code = main()
    if getattr(sys, "frozen", False):
        # frozen windowed builds can stall in interpreter teardown (Qt +
        # asyncio thread bits without a console). Everything durable is
        # already flushed at this point, so exit the process directly.
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:                    # noqa: BLE001
                pass
        os._exit(code)
    return code


if __name__ == "__main__":
    sys.exit(run())
