from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers import sample_new_draft
from thesis_review.paths import gui_dir
from thesis_review.settings import AppSettings, apply_model, load_settings, public_settings, save_settings
from thesis_review.word.adapter import WordAdapter

sys.modules.setdefault("webview", MagicMock())
from thesis_review.gui.app import Bridge  # noqa: E402


def test_gui_is_teacher_workstation_with_decision_filters():
    html = (gui_dir() / "ui.html").read_text(encoding="utf-8")
    js = (gui_dir() / "ui.js").read_text(encoding="utf-8")
    css = (gui_dir() / "ui.css").read_text(encoding="utf-8")
    assert "开始 AI 初审" in html
    assert "学生也可以自己先跑" not in html
    assert "待处理" in html
    assert "已确认" in html
    assert "已驳回" in html
    assert "批量接受格式问题" in html
    assert "生成正式审稿稿件" in html
    assert "打开日志文件夹" in html
    assert "btn-logs" in html
    assert "open_logs" in js
    assert "<details" in html
    assert "tech-log" in html
    assert "decision-tabs" in html
    assert "type-tabs" in html
    assert "stage-nav" in html
    assert "edited_accepted" in js
    assert "300" in js
    assert "teacher_decision !== \"pending\"" in js or 'decision === "pending"' in js
    assert "正式稿已生成" in js
    assert "stageBlockReason" in js
    assert "lastMissed" in js
    assert "formal === 0 || (current.pending || 0) > 0" in js
    assert "btn-export-confirmed" in js
    assert "编辑后确认需要填写老师最终意见" in js
    assert "text.trim() === original" in js
    assert "body.stage-prepare #sec-decide" in css
    assert "body.stage-reviewing #sec-prepare" in css
    assert "body.stage-decide #sec-prepare" in css
    assert "pointer-events: none" in css


def test_settings_persistence_does_not_leak_or_overwrite_key(tmp_path: Path):
    settings = AppSettings(
        api_key="sk-keep-me",
        model="gpt-4o-mini",
        last_paper_path=str(tmp_path / "paper.docx"),
        last_session_id="abc",
    )
    save_settings(tmp_path, settings)
    loaded = load_settings(tmp_path)
    assert loaded.api_key == "sk-keep-me"
    assert loaded.last_paper_path.endswith("paper.docx")
    assert loaded.last_session_id == "abc"
    apply_model(loaded, {"model": "gpt-4o-mini", "api_key": ""})
    save_settings(tmp_path, loaded)
    again = load_settings(tmp_path)
    assert again.api_key == "sk-keep-me"
    visible = public_settings(again)
    assert visible["api_key"] == ""
    assert "sk-keep-me" not in str(visible)
    dumped = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-keep-me" in dumped


def test_bridge_exposes_log_dir(tmp_path: Path):
    bridge = Bridge(tmp_path)
    payload = bridge.state()
    log_path = Path(payload["log_dir"])
    assert log_path == tmp_path / "logs"
    assert log_path.is_dir()
    assert (log_path / "run.log").is_file()
    assert "gui start" in (log_path / "run.log").read_text(encoding="utf-8")


def test_bridge_decide_export_only_writes_accepted(tmp_path: Path):
    draft = tmp_path / "new.docx"
    draft.write_bytes(sample_new_draft())
    bridge = Bridge(tmp_path)
    bridge._window = object()
    bridge._pick = lambda *, multiple: [str(draft)]  # type: ignore[method-assign]
    bridge._default_output = lambda: tmp_path / "out"  # type: ignore[method-assign]
    started = bridge.review_file()
    assert started["started"] is True
    deadline = time.time() + 20
    while time.time() < deadline:
        if bridge.progress().get("done"):
            break
        time.sleep(0.05)
    prog = bridge.progress()
    assert prog["done"] is True
    assert prog["reviewed_path"] in {"", None}
    findings = prog["findings"]
    assert findings
    blocked = bridge.export_final(False)
    assert blocked.get("needs_confirm") is True
    first = findings[0]
    second = findings[1] if len(findings) > 1 else None
    bridge.decide_finding(first["id"], "accepted")
    if second:
        bridge.decide_finding(second["id"], "rejected")
    bridge.accept_format_batch()
    exported = bridge.export_final(True)
    assert exported["ok"] is True
    comments = WordAdapter().extract_comments(WordAdapter().open_path(exported["reviewed_path"]))
    blob = "\n".join(item.text for item in comments)
    assert first["problem"] in blob
    if second:
        assert second["problem"] not in blob
    resumed = Bridge(tmp_path)
    assert resumed.session is not None
    assert resumed.findings
    assert resumed.recall.get("confirmed", 0) >= 0


def test_failed_review_marks_session_failed(tmp_path: Path):
    draft = tmp_path / "new.docx"
    draft.write_bytes(sample_new_draft())
    bridge = Bridge(tmp_path)
    bridge._window = object()
    bridge._pick = lambda *, multiple: [str(draft)]  # type: ignore[method-assign]
    bridge._default_output = lambda: tmp_path / "out"  # type: ignore[method-assign]

    def boom(*_args, **_kwargs):
        raise RuntimeError("pi exploded")

    bridge.service.review = boom  # type: ignore[method-assign]
    started = bridge.review_file()
    assert started["started"] is True
    deadline = time.time() + 10
    while time.time() < deadline:
        if bridge.progress().get("done"):
            break
        time.sleep(0.05)
    prog = bridge.progress()
    assert prog["done"] is True
    assert prog["error"]
    assert bridge.session is not None
    stored = bridge.service.sessions.get(bridge.session.id)
    assert stored.status == "failed"


def test_app_entry_routes_worker_cli_and_gui(monkeypatch):
    from thesis_review.gui import app as gui_app

    monkeypatch.setattr(gui_app, "worker_main", lambda argv: 11)
    monkeypatch.setattr(gui_app, "cli_main", lambda argv: 22)
    monkeypatch.setattr(gui_app, "start_gui", lambda: 33)
    monkeypatch.setattr(sys, "argv", ["论文审改助手.exe", "worker", "--home", "x"])
    assert gui_app.main() == 11
    monkeypatch.setattr(sys, "argv", ["论文审改助手.exe", "--home", "h", "demo", "--out", "o"])
    assert gui_app.main() == 22
    monkeypatch.setattr(sys, "argv", ["论文审改助手.exe"])
    assert gui_app.main() == 33


def test_open_reviewed_reports_missing_word_file(tmp_path: Path):
    bridge = Bridge(tmp_path)
    bridge.reviewed_path = str(tmp_path / "missing-reviewed.docx")
    result = bridge.open_reviewed()
    assert result["ok"] is False
    assert "找不到" in result["message"]


def test_gui_window_disables_easy_drag_and_allows_text_select():
    from thesis_review.gui import app as gui_app

    src = Path(gui_app.__file__).read_text(encoding="utf-8")
    assert "easy_drag=False" in src
    assert "text_select=True" in src
    assert "bridge._window = window" in src
    assert "def __dir__(self)" in src


def test_default_output_does_not_mkdir_on_init(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("thesis_review.gui.app.Path.home", lambda: tmp_path)
    documents = tmp_path / "Documents" / "论文审改结果"
    bridge = Bridge(tmp_path)
    assert bridge._default_output() == documents
    assert not documents.exists()
