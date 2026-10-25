"""Entry point: `python -m app.gui` (or the packaged WebsiteDownloader.exe,
which forwards --cli arguments to the CLI for scripted crawls)."""
import os
import sys


def main() -> int:
    if "--cli" in sys.argv:
        # packaged windowed builds have no console; still allow scripted runs
        from app.cli import main as cli_main

        return cli_main([a for a in sys.argv[1:] if a != "--cli"])
    from PySide6.QtWidgets import QApplication

    from app import __version__
    from app.gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Website Downloader")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
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
