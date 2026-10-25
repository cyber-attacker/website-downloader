"""Packaging entry point: PyInstaller follows this file's imports and bundles
the whole `app` package (engine + GUI). Also lets the packaged windowed exe
run scripted crawls: WebsiteDownloader.exe --cli <url> --depth 2 -o mirror
"""
from app.gui.__main__ import run

if __name__ == "__main__":
    raise SystemExit(run())
