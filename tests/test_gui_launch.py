"""First-launch path: classic start script + desktop bridge wait."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from thesis_review.paths import gui_dir, repo_root

sys.modules.setdefault("webview", MagicMock())
from thesis_review.gui.app import Bridge  # noqa: E402


def test_start_gui_script_falls_back_to_sibling_venv():
    script = (repo_root() / "scripts" / "start_gui.ps1").read_text(encoding="utf-8")
    assert r".venv\Scripts\python.exe" in script
    assert "thesis-review-agent" in script
    assert "THESIS_REVIEW_ROOT" in script
    assert "PYTHONPATH" in script
    assert "THESIS_REVIEW_PYTHON" in script
    cmd = repo_root() / "scripts" / "start_gui.cmd"
    assert cmd.is_file()
    cmd_text = cmd.read_text(encoding="utf-8")
    assert "ExecutionPolicy Bypass" in cmd_text
    assert "start_gui.ps1" in cmd_text


def test_ui_js_waits_for_desktop_bridge_instead_of_demo():
    js = (gui_dir() / "ui.js").read_text(encoding="utf-8")
    assert "function inDesktopShell" in js
    assert "chrome.webview" in js
    assert "BRIDGE_WAIT_DESKTOP_MS" in js
    assert "if (inDesktopShell()) return true" in js or "inDesktopShell()" in js
    assert "applyState(demoPrepareState())" in js
    # Desktop WebView2 must not take the 400ms browser-preview shortcut.
    assert "inDesktopShell()" in js.split("async function requireBridge")[1][:800]


def test_bridge_js_api_hides_webview_native(tmp_path: Path):
    class FakeNative:
        def __dir__(self):
            raise AssertionError("pywebview must not walk window.native")

        def __getattr__(self, name):
            raise AssertionError(f"pywebview must not read window.native.{name}")

    class FakeWindow:
        native = FakeNative()

    bridge = Bridge(tmp_path)
    bridge._window = FakeWindow()
    names = dir(bridge)
    assert "state" in names
    assert "add_missed_issue" in names
    assert "window" not in names
    assert "service" not in names
    assert "settings" not in names
    for name in names:
        attr = getattr(bridge, name)
        assert not hasattr(attr, "native")


def test_state_survives_store_errors(tmp_path: Path):
    bridge = Bridge(tmp_path)

    def boom(**_kwargs):
        raise RuntimeError("sqlite locked")

    bridge.service.store.list_issues = boom  # type: ignore[method-assign]
    payload = bridge.state()
    assert payload["stage"]
    assert "刷新失败" in (payload.get("warning") or payload.get("status") or "")
    assert payload.get("findings") == [] or isinstance(payload.get("findings"), list)
