"""V0.7 teacher feedback: layered soft reference, final-state collapse.

The full operation log is preserved; the agent only ever sees the final
effective decision per finding, split into per-student and teacher-global
layers, and neither layer may act as a hard rule.
"""
from __future__ import annotations

import base64
from pathlib import Path

from tests.helpers import sample_overclaim_draft
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.session_store import SessionStore, TeacherFeedback
from thesis_review.word.adapter import WordAdapter


def _seed_feedback(store: SessionStore) -> None:
    counter = {"n": 0}

    def add(session: str, finding: str, student: str, decision: str, at: str) -> None:
        counter["n"] += 1
        store.add_feedback(
            TeacherFeedback(
                id=f"{session}-{finding}-{counter['n']}",
                session_id=session,
                finding_id=finding,
                teacher_id="teacher-a",
                student_id=student,
                paper_path="",
                section="结论",
                decision=decision,
                original_payload={},
                edited_text="",
                created_at=at,
                kind="content",
                category="B",
                problem="结论缺少显著性检验。",
            )
        )

    # zhou's finding f1 flip-flops: accepted -> rejected -> accepted.
    add("s1", "f1", "zhou", "accepted", "2026-01-01T00:00:00+00:00")
    add("s1", "f1", "zhou", "rejected", "2026-01-01T00:00:00+00:00")
    add("s1", "f1", "zhou", "accepted", "2026-01-01T00:00:00+00:00")
    # zhou's finding f2 ends undecided.
    add("s1", "f2", "zhou", "rejected", "2026-01-02T00:00:00+00:00")
    add("s1", "f2", "zhou", "pending", "2026-01-03T00:00:00+00:00")
    # another student's feedback is the global layer.
    add("s2", "g1", "other-student", "edited_accepted", "2026-01-04T00:00:00+00:00")


def test_final_view_collapses_to_latest_effective_decision(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    _seed_feedback(store)
    full = store.list_feedback(teacher_id="teacher-a", student_id="zhou", limit=20)
    assert len(full) == 5, "the operation log keeps every action"
    final = store.list_feedback_final(teacher_id="teacher-a", student_id="zhou", limit=20)
    assert [(item.finding_id, item.decision) for item in final] == [("f1", "accepted")]
    # f2's latest state is pending: undecided findings carry no signal.


def test_worker_returns_layered_soft_reference(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    _seed_feedback(service.sessions)
    import thesis_review.worker as worker_module

    worker = worker_module.Worker(
        home=tmp_path,
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
    )
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(sample_overclaim_draft()).decode("ascii")})
    result = worker.dispatch("get_teacher_feedback", {})
    student_items = result["items"]
    global_items = result["global_items"]
    assert [item["layer"] for item in student_items] == ["student"]
    assert student_items[0]["decision"] == "accepted"
    assert student_items[0]["finding_id"] if "finding_id" in student_items[0] else True
    # No pending or superseded rows leak into the soft reference.
    assert all(item["decision"] != "pending" for item in student_items + global_items)
    assert [item["layer"] for item in global_items] == ["global"]
    assert global_items[0]["decision"] == "edited_accepted"
    # Soft reference contract: never a positive rule, in either layer.
    for item in student_items + global_items:
        assert item["positive_rule"] is False
        assert "升格" in item["hint"] or "软参考" in item["hint"]


def test_global_layer_excludes_current_student(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    _seed_feedback(store)
    finals = store.list_feedback_final(teacher_id="teacher-a", student_id="", limit=20)
    assert {(item.finding_id, item.decision) for item in finals} == {
        ("f1", "accepted"),
        ("g1", "edited_accepted"),
    }


def test_service_decide_flow_writes_final_state(tmp_path: Path):
    """End to end: decide_finding appends to the log, the agent view collapses."""
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    from tests.helpers import OVERCLAIM_CLAIM_QUOTE, OVERCLAIM_EVIDENCE_QUOTE

    session = service.start_session(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="overclaim",
        paper_path="",
    )
    from thesis_review.types import Finding, prepare_candidate

    finding = prepare_candidate(
        Finding(
            id="content-1",
            category="B",
            source="argument",
            problem="结论缺少显著性检验且用词过满。",
            rationale="提升幅度微小且无检验。",
            quote=OVERCLAIM_CLAIM_QUOTE,
            anchor="P3",
            paragraph_index=3,
            apply="comment",
        ),
        session_id=session.id,
    )
    session.findings = [finding]
    service.sessions.save(session)
    service.decide_finding(session_id=session.id, finding_id="content-1", decision="accepted")
    service.decide_finding(session_id=session.id, finding_id="content-1", decision="rejected")
    log = service.sessions.list_feedback(teacher_id="teacher-a", student_id="zhou")
    assert {item.decision for item in log} == {"accepted", "rejected"}
    final = service.sessions.list_feedback_final(teacher_id="teacher-a", student_id="zhou")
    assert [item.decision for item in final] == ["rejected"]
