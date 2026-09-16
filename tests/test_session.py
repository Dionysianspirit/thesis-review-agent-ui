from __future__ import annotations

from pathlib import Path

from tests.helpers import sample_history_v1, sample_new_draft
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.session_store import SessionStore, model_snapshot
from thesis_review.settings import AppSettings
from thesis_review.types import DECISION_ACCEPTED
from thesis_review.word.adapter import WordAdapter


def test_session_saves_and_restores_findings_and_decisions(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=sample_new_draft(),
        output_dir=tmp_path / "out",
        use_model=False,
        paper_path=str(tmp_path / "new.docx"),
    )
    (tmp_path / "new.docx").write_bytes(sample_new_draft())
    finding = result.findings[0]
    service.decide_finding(session_id=result.session_id, finding_id=finding.id, decision=DECISION_ACCEPTED)
    exported = service.export_final(session_id=result.session_id, allow_pending=True)
    restored = SessionStore(tmp_path / "review-sessions.sqlite")
    session = restored.get(result.session_id)
    assert session.paper_path
    assert session.findings
    assert any(item.teacher_decision == DECISION_ACCEPTED for item in session.findings)
    assert session.final_output_path == exported["reviewed_path"]
    assert session.completed is True
    assert "api_key" not in session.model_snapshot
    assert Path(session.final_output_path).is_file()


def test_history_drafts_survive_restart_and_missing_file(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    history = tmp_path / "v1.docx"
    history.write_bytes(sample_history_v1())
    records = service.ingest_history(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="v1",
        data=history.read_bytes(),
        source_path=str(history),
    )
    assert records
    history.unlink()
    restored = SessionStore(tmp_path / "review-sessions.sqlite")
    drafts = restored.list_history_drafts(teacher_id="teacher-a", student_id="zhou")
    assert drafts
    assert drafts[0].accessible is False
    assert drafts[0].issue_count >= 1
    issues = service.store.list_issues(teacher_id="teacher-a", student_id="zhou")
    assert issues


def test_list_history_drafts_can_skip_access_probe(tmp_path: Path, monkeypatch):
    from thesis_review.session_store import HistoryDraftRecord, SessionStore

    store = SessionStore(tmp_path / "review-sessions.sqlite")
    store.add_history_draft(
        HistoryDraftRecord(
            id="d1",
            teacher_id="teacher-a",
            student_id="zhou",
            draft_id="v1",
            path=str(tmp_path / "missing.docx"),
            imported_at="t",
            issue_count=1,
        )
    )

    def forbidden(_path):
        raise AssertionError("state() must not probe history draft paths on first paint")

    monkeypatch.setattr("thesis_review.session_store.path_is_file", forbidden)
    drafts = store.list_history_drafts(teacher_id="teacher-a", student_id="zhou", check_access=False)
    assert drafts
    assert drafts[0].path.endswith("missing.docx")


def test_list_recent_briefs_omit_findings(tmp_path: Path):
    from thesis_review.session_store import SESSION_AWAITING, ReviewSession
    from thesis_review.types import Finding

    store = SessionStore(tmp_path / "review-sessions.sqlite")
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
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path="",
        created_at="t",
        updated_at="t",
        status=SESSION_AWAITING,
        findings=[finding],
    )
    store.save(session)
    briefs = store.list_recent_briefs(teacher_id="teacher-a", limit=8)
    assert briefs
    assert briefs[0]["id"] == "s1"
    assert "findings" not in briefs[0]


def test_path_is_file_local(tmp_path: Path):
    from thesis_review.paths import path_is_file

    missing = tmp_path / "gone.docx"
    assert path_is_file(missing) is False
    missing.write_text("x", encoding="utf-8")
    assert path_is_file(missing) is True
    assert path_is_file("") is False


def test_model_snapshot_never_stores_key(tmp_path: Path):
    snap = model_snapshot(AppSettings(api_key="sk-secret", model="gpt-4o-mini", provider="openai-compatible"))
    assert snap["api_key_set"] is True
    assert "api_key" not in snap
    assert "sk-secret" not in str(snap)
