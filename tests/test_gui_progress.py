from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers import sample_new_draft
from thesis_review.paths import gui_dir
from thesis_review.types import ReviewResult

sys.modules.setdefault("webview", MagicMock())

from thesis_review.gui.app import Bridge  # noqa: E402


def test_gui_shell_has_collapsible_tech_log_and_progress_poll():
    html = (gui_dir() / "ui.html").read_text(encoding="utf-8")
    js = (gui_dir() / "ui.js").read_text(encoding="utf-8")
    assert "<details" in html
    assert "tech-log" in html
    assert "progress" in js
    assert "300" in js
    assert "started" in js
    assert "item.label" in js
    assert "未完成" in js
    assert "open_logs" in js


def test_bridge_progress_reads_partial_live(tmp_path: Path):
    from thesis_review.live import append_event, write_findings

    live = tmp_path / "out" / "live"
    append_event(live, {"op": "open_draft", "ok": True})
    append_event(live, {"op": "run_checks", "ok": True})
    write_findings(live, [{"id": "rule-A-1", "source": "rule", "category": "A", "problem": "叠词。"}])
    bridge = Bridge(tmp_path)
    bridge._live_dir = live
    payload = bridge.progress()
    assert "正在" in payload["message"] or "核对" in payload["message"]
    assert payload["findings"][0]["id"] == "rule-A-1"
    assert payload["done"] is False
    assert any(item["op"] == "run_checks" for item in payload["tech_log"])
    assert "api_key" not in str(payload)


def test_review_file_returns_started_without_blocking(tmp_path: Path):
    draft = tmp_path / "new.docx"
    draft.write_bytes(sample_new_draft())
    bridge = Bridge(tmp_path)
    bridge._window = object()
    bridge._pick = lambda *, multiple: [str(draft)]  # type: ignore[method-assign]
    bridge._default_output = lambda: tmp_path / "out"  # type: ignore[method-assign]

    release = threading.Event()
    entered = threading.Event()

    def slow_review(**_kwargs):
        entered.set()
        release.wait(timeout=5)
        reviewed = tmp_path / "out" / "new-reviewed.docx"
        findings = tmp_path / "out" / "new-findings.json"
        reviewed.parent.mkdir(parents=True, exist_ok=True)
        reviewed.write_bytes(sample_new_draft())
        findings.write_text("[]", encoding="utf-8")
        return ReviewResult(
            reviewed_path=reviewed,
            findings_path=findings,
            findings=[],
            used_model=False,
        )

    bridge.service.review = slow_review  # type: ignore[method-assign]
    result = bridge.review_file()
    assert result.get("started") is True
    assert entered.wait(timeout=2)
    mid = bridge.progress()
    assert mid["done"] is False
    release.set()
    deadline = time.time() + 3
    while time.time() < deadline:
        if bridge.progress().get("done"):
            break
        time.sleep(0.05)
    done = bridge.progress()
    assert done["done"] is True
    assert done["reviewed_path"] in {"", None}
    assert (tmp_path / "settings.json").is_file()
