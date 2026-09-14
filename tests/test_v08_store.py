"""V0.8 store layer: V0.7 compatibility, rejection reasons, missed issues."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.helpers import sample_new_draft
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.session_store import SessionStore
from thesis_review.types import (
    DECISION_ACCEPTED,
    DECISION_PENDING,
    DECISION_REJECTED,
    Finding,
    is_exportable,
)
from thesis_review.word.adapter import WordAdapter


def _finding(id: str, decision: str = DECISION_PENDING, **extra) -> Finding:
    return Finding(
        id=id,
        category="B",
        source="content",
        kind="content",
        subtype="argument",
        problem=f"问题 {id}",
        rationale="依据。",
        quote="原文",
        anchor="P0001",
        paragraph_index=1,
        apply="comment",
        teacher_decision=decision,
        **extra,
    )


def _service(tmp_path: Path) -> ThesisReviewService:
    return ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )


def test_pre_v08_database_rows_survive_and_load(tmp_path: Path):
    """A V0.7 sessions table (no eval columns) gains the columns, keeps rows."""
    path = tmp_path / "review-sessions.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL,
            teacher_name TEXT NOT NULL DEFAULT '',
            student_id TEXT NOT NULL,
            student_name TEXT NOT NULL DEFAULT '',
            major TEXT NOT NULL DEFAULT '',
            draft_id TEXT NOT NULL DEFAULT '',
            paper_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            model_snapshot TEXT NOT NULL DEFAULT '{}',
            findings TEXT NOT NULL DEFAULT '[]',
            final_output_path TEXT NOT NULL DEFAULT '',
            completed INTEGER NOT NULL DEFAULT 0,
            source_path TEXT NOT NULL DEFAULT '',
            output_dir TEXT NOT NULL DEFAULT '',
            quality TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (
            id, teacher_id, student_id, draft_id, paper_path, created_at, updated_at, status
        ) VALUES ('v07-session', 'teacher-a', 'zhou', 'new', 'p.docx',
                  '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', 'completed')
        """
    )
    conn.commit()
    conn.close()
    store = SessionStore(path)
    session = store.get("v07-session")
    assert session.findings == []
    assert session.missed_issues == []
    assert session.review_started_at == ""
    assert session.token_usage == {}
    # Saving a pre-V0.8 row again must not lose data.
    session.status = "awaiting_teacher"
    store.save(session)
    assert store.get("v07-session").status == "awaiting_teacher"


def test_old_finding_dict_without_eval_fields_loads_clean():
    finding = Finding.from_dict({"id": "old", "problem": "旧候选", "quote": "原文"})
    assert finding.rejection_reason == ""
    assert finding.rejection_note == ""
    assert finding.teacher_decision == DECISION_PENDING


def test_rejection_reason_roundtrip_and_update(tmp_path: Path):
    store = SessionStore(tmp_path / "review-sessions.sqlite")
    session = _session(tmp_path, store, findings=[_finding("f1")])
    store.set_decision(
        session_id=session.id,
        finding_id="f1",
        decision=DECISION_REJECTED,
        rejection_reason="false_positive",
        rejection_note="原文其实有基线。",
    )
    restored = SessionStore(tmp_path / "review-sessions.sqlite").get(session.id)
    assert restored.findings[0].teacher_decision == DECISION_REJECTED
    assert restored.findings[0].rejection_reason == "false_positive"
    assert restored.findings[0].rejection_note == "原文其实有基线。"
    # Teacher revises the annotation later. set_rejection_reason replaces the
    # whole annotation (the UI submits reason and note together), so the note
    # the teacher cleared disappears with it; the decision stays untouched.
    store.set_rejection_reason(session_id=session.id, finding_id="f1", rejection_reason="duplicate")
    again = store.get(session.id).findings[0]
    assert again.teacher_decision == DECISION_REJECTED
    assert again.rejection_reason == "duplicate"
    assert again.rejection_note == ""


def test_rejection_reason_is_optional_and_validated(tmp_path: Path):
    store = SessionStore(tmp_path / "review-sessions.sqlite")
    session = _session(tmp_path, store, findings=[_finding("f1")])
    store.set_decision(session_id=session.id, finding_id="f1", decision=DECISION_REJECTED)
    assert store.get(session.id).findings[0].rejection_reason == ""
    try:
        store.set_decision(session_id=session.id, finding_id="f1", decision=DECISION_REJECTED, rejection_reason="nonsense")
        raise AssertionError("invalid reason must be rejected")
    except ValueError:
        pass


def test_rejection_reason_never_changes_teacher_gate(tmp_path: Path):
    finding = _finding("f1", DECISION_REJECTED, rejection_reason="false_positive", rejection_note="备注")
    assert is_exportable(finding) is False
    service = _service(tmp_path)
    store = service.sessions
    session = _session(tmp_path, store, findings=[finding])
    store.set_decision(session_id=session.id, finding_id="f1", decision=DECISION_REJECTED)
    (tmp_path / "new.docx").write_bytes(sample_new_draft())
    session.source_path = str(tmp_path / "new.docx")
    store.save(session)
    result = service.export_final(session_id=session.id, allow_pending=True)
    assert result["ok"] is True
    assert result["n_exported"] == 0


def test_set_rejection_reason_does_not_append_feedback_row(tmp_path: Path):
    service = _service(tmp_path)
    result_findings = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=sample_new_draft(),
        output_dir=tmp_path / "out",
        use_model=False,
        paper_path=str(tmp_path / "new.docx"),
    ).findings
    target = result_findings[0]
    service.decide_finding(
        session_id=result_findings[0].session_id,
        finding_id=target.id,
        decision=DECISION_REJECTED,
    )
    session_id = result_findings[0].session_id
    before = len(service.sessions.list_feedback(teacher_id="teacher-a", limit=100))
    service.set_rejection_reason(
        session_id=session_id,
        finding_id=target.id,
        rejection_reason="not_important",
        rejection_note="不构成正式意见。",
    )
    after = len(service.sessions.list_feedback(teacher_id="teacher-a", limit=100))
    assert before == after
    assert service.sessions.get(session_id).findings[0].rejection_reason == "not_important"


def test_missed_issues_roundtrip_and_eval_isolation(tmp_path: Path):
    service = _service(tmp_path)
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=sample_new_draft(),
        output_dir=tmp_path / "out",
        use_model=False,
        paper_path=str(tmp_path / "new.docx"),
    )
    session_id = result.session_id
    issue = service.add_missed_issue(
        session_id=session_id,
        problem="AI 漏掉的数据矛盾",
        section="2 方法",
        category="content",
        note="老师课后发现。",
    )
    restored = SessionStore(tmp_path / "review-sessions.sqlite").get(session_id)
    assert len(restored.missed_issues) == 1
    assert restored.missed_issues[0].problem == "AI 漏掉的数据矛盾"
    assert restored.missed_issues[0].source == "teacher_missed_issue"
    assert restored.missed_issues[0].id == issue.id
    # Missed issues stay out of the AI candidate list and its metrics.
    assert all(item.id != issue.id for item in restored.findings)
    metrics = _metrics(restored)
    assert metrics["candidate_count"] == len(restored.findings)
    assert metrics["missed_issue_count"] == 1
    service.remove_missed_issue(session_id=session_id, issue_id=issue.id)
    assert service.sessions.get(session_id).missed_issues == []


def test_add_missed_issue_requires_problem(tmp_path: Path):
    service = _service(tmp_path)
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=sample_new_draft(),
        output_dir=tmp_path / "out",
        use_model=False,
        paper_path=str(tmp_path / "new.docx"),
    )
    try:
        service.add_missed_issue(session_id=result.session_id, problem="   ")
        raise AssertionError("empty problem must be rejected")
    except Exception as exc:  # noqa: BLE001 - ReviewError contract
        assert getattr(exc, "code", "") == "invalid_decision"


def test_pre_v08_session_still_exports_eval(tmp_path: Path):
    path = tmp_path / "review-sessions.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            teacher_id TEXT NOT NULL,
            teacher_name TEXT NOT NULL DEFAULT '',
            student_id TEXT NOT NULL,
            student_name TEXT NOT NULL DEFAULT '',
            major TEXT NOT NULL DEFAULT '',
            draft_id TEXT NOT NULL DEFAULT '',
            paper_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            model_snapshot TEXT NOT NULL DEFAULT '{}',
            findings TEXT NOT NULL DEFAULT '[]',
            final_output_path TEXT NOT NULL DEFAULT '',
            completed INTEGER NOT NULL DEFAULT 0,
            source_path TEXT NOT NULL DEFAULT '',
            output_dir TEXT NOT NULL DEFAULT '',
            quality TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    findings_json = '[{"id": "old-1", "problem": "旧候选", "quote": "原文", "paragraph_index": 1, "teacher_decision": "accepted"}]'
    conn.execute(
        """
        INSERT INTO sessions (
            id, teacher_id, student_id, draft_id, paper_path, created_at, updated_at,
            status, findings, output_dir, completed
        ) VALUES ('v07-session', 'teacher-a', 'zhou', 'new', 'p.docx',
                  '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',
                  'completed', ?, ?, 1)
        """,
        (findings_json, str(tmp_path / "out")),
    )
    conn.commit()
    conn.close()
    service = _service(tmp_path)
    result = service.export_eval(session_id="v07-session")
    assert result["ok"] is True
    assert Path(result["files"]["json"]).is_file()
    assert Path(result["files"]["findings_csv"]).is_file()
    assert Path(result["files"]["summary_csv"]).is_file()


def _session(tmp_path: Path, store: SessionStore, *, findings: list[Finding]):
    from thesis_review.session_store import ReviewSession

    session = ReviewSession(
        id="s-eval",
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path=str(tmp_path / "new.docx"),
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T00:00:00+00:00",
        findings=findings,
    )
    store.create(session)
    return session


def _metrics(session):
    from thesis_review.evalmetrics import compute_eval_metrics

    return compute_eval_metrics(session.findings, session.missed_issues, None, session)
