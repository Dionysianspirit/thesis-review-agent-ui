from __future__ import annotations

import os
import sys
import threading
from pathlib import Path


def repo_root() -> Path:
    override = os.environ.get("THESIS_REVIEW_ROOT")
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scripts" / "fetch_docxengine.py").is_file():
            return parent
    return here.parents[2]


def vendor_src() -> Path:
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "docxengine"
        if bundled.is_dir():
            return bundled
        beside = Path(sys.executable).resolve().parent / "docxengine"
        if beside.is_dir():
            return beside
    return repo_root() / ".vendor" / "docxengine" / "src"


def app_home() -> Path:
    override = os.environ.get("THESIS_REVIEW_HOME")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "ThesisReviewAgent"
    return Path.home() / ".thesis-review-agent"


def gui_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "thesis_review" / "gui"
    return Path(__file__).resolve().parent / "gui"


def path_is_file(path: str | Path | None, timeout: float = 0.4) -> bool:
    """Return whether *path* is a file, without hanging on dead UNC/OneDrive roots."""
    text = str(path or "").strip()
    if not text:
        return False
    done = threading.Event()
    result = {"ok": False}

    def check() -> None:
        try:
            result["ok"] = Path(text).is_file()
        except OSError:
            result["ok"] = False
        finally:
            done.set()

    threading.Thread(target=check, daemon=True).start()
    if not done.wait(timeout):
        return False
    return bool(result["ok"])
