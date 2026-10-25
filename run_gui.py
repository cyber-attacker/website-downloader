#!/usr/bin/env python3
"""Double-clickable launcher for Website Downloader.

Works from a bare double-click even on Python installs that don't have the
dependencies:

1. re-executes through the project's `.venv` when one exists
2. otherwise offers to create it (one dialog) — `python -m venv` +
   `pip install -r requirements.txt` — then relaunches
3. `--cli …` arguments pass straight to the CLI for scripted crawls

For a real distribution, build the exe instead:
`packaging\\build_windows.ps1`
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def venv_python(gui: bool) -> str:
    """The venv interpreter: pythonw.exe for the GUI (no console flash),
    python.exe for --cli so output is visible."""
    return os.path.join(HERE, ".venv", "Scripts",
                        "pythonw.exe" if gui else "python.exe")


def in_project_venv() -> bool:
    return os.path.normpath(sys.prefix).lower().startswith(
        os.path.normpath(os.path.join(HERE, ".venv")).lower())


def _have_deps() -> bool:
    try:
        import PySide6  # noqa: F401
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def _notice(title: str, message: str, question: bool = False) -> bool:
    """Console-free user feedback via tkinter (stdlib on Windows)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        if question:
            ok = messagebox.askyesno(title, message, icon="question")
            root.destroy()
            return ok
        messagebox.showinfo(title, message, icon="info")
        root.destroy()
        return False
    except Exception:                        # no tkinter / no display
        print(message, file=sys.stderr)
        return False


def _bootstrap_venv() -> bool:
    """Create .venv and install requirements, visibly. Returns True on success."""
    print("Creating .venv and installing dependencies (one time only)…")
    try:
        subprocess.run([sys.executable, "-m", "venv", os.path.join(HERE, ".venv")],
                       check=True, cwd=HERE)
        proc = subprocess.run(
            [venv_python(gui=False), "-m", "pip", "install",
             "--upgrade", "pip", "-r", os.path.join(HERE, "requirements.txt")],
            cwd=HERE)
        return proc.returncode == 0
    except Exception as exc:                # noqa: BLE001 — surfaced to the user
        _notice("Website Downloader",
                f"Could not set up the environment:\n{exc}\n\n"
                "Create it manually with:\n"
                "    python -m venv .venv\n"
                "    .venv\\Scripts\\pip install -r requirements.txt")
        return False


def run() -> int:
    args = [a for a in sys.argv[1:] if a != "--cli"]
    is_cli = "--cli" in sys.argv
    # a lone --help/-h means CLI usage, not a GUI launch
    if not is_cli and any(a in ("-h", "--help") for a in sys.argv[1:]):
        is_cli = True

    # already inside the project venv (or deps importable): run directly
    if in_project_venv() or (not is_cli and _have_deps()):
        if is_cli:
            return _run_cli(args)
        from app.gui.__main__ import run as run_app

        return run_app()

    gui = not is_cli
    exe = venv_python(gui)
    if os.path.exists(exe):
        # venv exists but we were started by another interpreter — re-exec
        argv = [exe, os.path.join(HERE, "run_gui.py")] + sys.argv[1:]
        proc = subprocess.Popen(argv, cwd=HERE)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 130

    # no venv: offer the one-time bootstrap, then relaunch
    if _notice("Website Downloader",
               "First run: this needs a local Python environment "
               "(.venv) with the app's dependencies.\n\n"
               "Create it now? (~1–2 minutes, once)", question=True):
        if _bootstrap_venv():
            argv = [venv_python(gui), os.path.join(HERE, "run_gui.py")] + sys.argv[1:]
            return subprocess.Popen(argv, cwd=HERE).wait()
        return 1
    _notice("Website Downloader",
            "No problem. To start the app later, run:\n"
            "    packaging\\build_windows.ps1   (builds WebsiteDownloader.exe)\n"
            "or set up the environment as described in README.md.")
    return 1


def _run_cli(args: list[str]) -> int:
    from app.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    sys.exit(run())
