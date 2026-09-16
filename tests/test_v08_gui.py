"""V0.8 GUI bridge: eval summary surface, rejection annotation, missed issues."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers import sample_new_draft

sys.modules.setdefault("webview", MagicMock())

from thesis_review.gui.app import Bridge  # noqa: E402


def _reviewed_bridge(tmp_path: Path) -> Bridge:
    bridge = Bridge(tmp_path)
    draft = tmp_path / "new.docx"
    draft.write_bytes(sample_new_draft())
    result = bridge.service.review(
        teacher_id=bridge.settings.teacher_id,
        student_id=bridge.settings.student_id,
        draft_id="new",
        data=draft.read_bytes(),
        output_dir=tmp_path / "out",
        use_model=False,
        paper_path=str(draft),
    )
    bridge.session = bridge.service.sessions.get(result.session_id)
    bridge.findings = [item.to_dict() for item in bridge.session.findings]
    return bridge


def test_state_carries_eval_summary_and_session_timing(tmp_path: Path):
    bridge = _reviewed_bridge(tmp_path)
    state = bridge.state()
    summary = state["eval_summary"]
    assert summary is not None
    assert summary["candidate_count"] == len(bridge.session.findings)
    assert summary["candidate_count"] > 0
    assert summary["pending_count"] == summary["candidate_count"]
    # No decision yet: rates must be unknown, not zero.
    assert summary["acceptance_rate"] is None
    assert summary["timing"]["review_started_at"]
    assert summary["timing"]["review_completed_at"]


def test_bridge_decide_with_rejection_reason_updates_summary(tmp_path: Path):
    bridge = _reviewed_bridge(tmp_path)
    finding = bridge.findings[0]
    result = bridge.decide_finding(
        finding["id"],
        "rejected",
        rejection_reason="false_positive",
        rejection_note="测试驳回。",
    )
    assert result["ok"] is True
    state = bridge.state()
    summary = state["eval_summary"]
    assert summary["rejected_count"] == 1
    assert summary["acceptance_rate"] == 0.0
    assert summary["rejection_reasons"].get("false_positive") == 1


def test_bridge_add_and_remove_missed_issue(tmp_path: Path):
    bridge = _reviewed_bridge(tmp_path)
    result = bridge.add_missed_issue(
        problem="AI 漏掉的数据矛盾",
        section="2 方法",
        category="content",
    )
    assert result["ok"] is True
    state = bridge.state()
    missed = state["session"]["missed_issues"]
    assert len(missed) == 1
    assert missed[0]["source"] == "teacher_missed_issue"
    # Missed issues never join the AI candidate metrics.
    assert state["eval_summary"]["missed_issue_count"] == 1
    assert state["eval_summary"]["candidate_count"] == len(state["findings"])
    removed = bridge.remove_missed_issue(missed[0]["id"])
    assert removed["ok"] is True
    assert bridge.state()["session"]["missed_issues"] == []


def test_bridge_add_missed_issue_requires_problem(tmp_path: Path):
    bridge = _reviewed_bridge(tmp_path)
    result = bridge.add_missed_issue(problem="  ")
    assert result["ok"] is False
    assert "问题描述" in result["message"]


def test_bridge_export_eval_writes_files(tmp_path: Path):
    bridge = _reviewed_bridge(tmp_path)
    finding = bridge.findings[0]
    bridge.decide_finding(finding["id"], "accepted")
    result = bridge.export_eval()
    assert result["ok"] is True
    json_path = Path(result["files"]["json"])
    assert json_path.name == "review-eval.json"
    assert json_path.is_file()
    assert Path(result["files"]["findings_csv"]).is_file()
    assert Path(result["files"]["summary_csv"]).is_file()


def test_ui_html_has_eval_panel_and_missed_form():
    from thesis_review.paths import gui_dir

    html = (gui_dir() / "ui.html").read_text(encoding="utf-8")
    js = (gui_dir() / "ui.js").read_text(encoding="utf-8")
    assert "missed-problem" in html
    assert "本次审稿评测" in html
    assert "AI 漏检补录" in html
    assert "不进入正式 Word" in html or "不会进入正式 Word" in html
    assert "renderEvalSummary" in js
    assert 'read: "已读到"' in js
    assert 'probed: "仅检索到"' in js
    assert 'unread: "未读到"' in js
    assert "不是通读全文" in js
    assert "全部章节均已覆盖" not in js
    assert "set_rejection_reason" in js
    assert "add_missed_issue" in js
    assert "export_eval" in js
    assert "lastMissed" in js
    assert "scrollIntoView" in js
    assert "已补录漏检" in js


def test_state_keeps_session_payload_light(tmp_path: Path):
    from thesis_review.session_store import SESSION_AWAITING, ReviewSession
    from thesis_review.types import Finding

    bridge = Bridge(tmp_path)
    finding = Finding(
        id="f1",
        category="A",
        source="rule",
        problem="叠词",
        rationale="口语化",
        quote="",
        anchor="P1",
        paragraph_index=1,
        apply="comment",
    )
    session = ReviewSession(
        id="s1",
        teacher_id=bridge.settings.teacher_id,
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path="",
        created_at="t",
        updated_at="t",
        status=SESSION_AWAITING,
        findings=[finding],
    )
    bridge.session = session
    bridge.findings = [finding.to_dict()]
    bridge.service.sessions.save(session)
    state = bridge.state()
    assert state["findings"]
    assert "findings" not in (state["session"] or {})
    assert "missed_issues" in (state["session"] or {})
    for item in state["sessions"]:
        assert "findings" not in item
        assert "id" in item
