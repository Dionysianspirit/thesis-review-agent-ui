"""V0.7 teacher-edited revision text: comment body and tracked replacement
are separate editable fields; the Teacher Gate semantics are unchanged."""
from __future__ import annotations

from pathlib import Path

from tests.helpers import sample_new_draft
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.types import DECISION_EDITED, Finding, prepare_candidate
from thesis_review.word.adapter import WordAdapter


def _revision_finding(anchor: str = "", paragraph_index: int = 2) -> Finding:
    return prepare_candidate(
        Finding(
            id="rule-C-1",
            category="C",
            source="rule",
            code="space_punctuation",
            problem="标点使用了半角逗号，应使用全角。",
            rationale="学校格式要求中文语境使用全角标点。",
            quote="本研究使用卷积神经网络（CNN）进行图像分类",
            anchor=anchor or "P2",
            paragraph_index=paragraph_index,
            apply="both",
            suggested_old="进行图像分类",
            suggested_new="完成图像分类任务",
        )
    )


def _real_anchor() -> str:
    adapter = WordAdapter()
    paragraphs = adapter.list_paragraphs(adapter.open_bytes(sample_new_draft()))
    return next(item.anchor for item in paragraphs if "进行图像分类" in item.text)


def _service(tmp_path: Path) -> tuple[ThesisReviewService, str]:
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    paper = tmp_path / "new.docx"
    paper.write_bytes(sample_new_draft())
    output = tmp_path / "out"
    output.mkdir()
    (output / "new-source.docx").write_bytes(sample_new_draft())
    session = service.start_session(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path=str(paper),
        output_dir=str(output),
    )
    session.source_path = str(output / "new-source.docx")
    service.sessions.save(session)
    return service, session.id


def test_edited_accept_separates_comment_and_revision_text(tmp_path: Path):
    service, session_id = _service(tmp_path)
    session = service.sessions.get(session_id)
    session.findings = [_revision_finding(anchor=_real_anchor())]
    service.sessions.save(session)
    finding = service.decide_finding(
        session_id=session_id,
        finding_id="rule-C-1",
        decision=DECISION_EDITED,
        edited_text="批注意见（老师修订版）：此处措辞需按学科规范调整。",
        edited_new_text="执行图像分类流程",
    )
    assert finding.teacher_final_text == "批注意见（老师修订版）：此处措辞需按学科规范调整。"
    assert finding.teacher_final_new == "执行图像分类流程"
    # Original suggestion stays untouched as provenance.
    assert finding.suggested_new == "完成图像分类任务"

    exported = service.export_final(session_id=session_id)
    assert exported["ok"] is True
    adapter = WordAdapter()
    opened = adapter.open_path(Path(exported["reviewed_path"]))
    revisions = adapter.extract_revisions(opened)
    inserted = [rev.text for rev in revisions if "执行图像分类流程" in rev.text]
    assert inserted, "tracked revision must use the teacher-edited replacement"
    assert all("完成图像分类任务" not in rev.text for rev in revisions)
    comments = adapter.extract_comments(opened)
    bodies = "\n".join(comment.text for comment in comments)
    assert "批注意见（老师修订版）" in bodies
    assert "执行图像分类流程" not in bodies, "comment body must not absorb revision text"


def test_edited_accept_without_new_text_keeps_suggested_replacement(tmp_path: Path):
    service, session_id = _service(tmp_path)
    session = service.sessions.get(session_id)
    session.findings = [_revision_finding(anchor=_real_anchor())]
    service.sessions.save(session)
    service.decide_finding(
        session_id=session_id,
        finding_id="rule-C-1",
        decision=DECISION_EDITED,
        edited_text="只改批注不改替换文本。",
    )
    exported = service.export_final(session_id=session_id)
    opened = WordAdapter().open_path(Path(exported["reviewed_path"]))
    revisions = WordAdapter().extract_revisions(opened)
    assert any("完成图像分类任务" in rev.text for rev in revisions)


def test_rejected_revision_still_never_writes(tmp_path: Path):
    service, session_id = _service(tmp_path)
    session = service.sessions.get(session_id)
    session.findings = [_revision_finding(anchor=_real_anchor())]
    service.sessions.save(session)
    service.decide_finding(
        session_id=session_id,
        finding_id="rule-C-1",
        decision="rejected",
    )
    exported = service.export_final(session_id=session_id, allow_pending=False)
    assert exported["ok"] is True
    opened = WordAdapter().open_path(Path(exported["reviewed_path"]))
    assert WordAdapter().extract_revisions(opened) == []
    assert WordAdapter().extract_comments(opened) == []


def test_teacher_final_new_survives_session_roundtrip(tmp_path: Path):
    service, session_id = _service(tmp_path)
    session = service.sessions.get(session_id)
    session.findings = [_revision_finding(anchor=_real_anchor())]
    service.sessions.save(session)
    service.decide_finding(
        session_id=session_id,
        finding_id="rule-C-1",
        decision=DECISION_EDITED,
        edited_text="老师批注。",
        edited_new_text="执行图像分类流程",
    )
    restored = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
        sessions=service.sessions,
    )
    reloaded = restored.sessions.get(session_id)
    assert reloaded.findings[0].teacher_final_new == "执行图像分类流程"
    assert reloaded.findings[0].teacher_final_text == "老师批注。"


def test_legacy_finding_without_new_field_still_exports(tmp_path: Path):
    """Sessions saved by V0.6 have no teacher_final_new; export must not change."""
    service, session_id = _service(tmp_path)
    session = service.sessions.get(session_id)
    finding = _revision_finding(anchor=_real_anchor())
    legacy = finding.to_dict()
    legacy.pop("teacher_final_new", None)
    from thesis_review.types import Finding as FindingCls

    session.findings = [FindingCls.from_dict(legacy)]
    service.sessions.save(session)
    service.decide_finding(
        session_id=session_id,
        finding_id="rule-C-1",
        decision=DECISION_EDITED,
        edited_text="旧会话恢复后的批注。",
    )
    exported = service.export_final(session_id=session_id)
    assert exported["ok"] is True
    opened = WordAdapter().open_path(Path(exported["reviewed_path"]))
    revisions = WordAdapter().extract_revisions(opened)
    assert any("完成图像分类任务" in rev.text for rev in revisions)


def test_new_draft_revision_smoke(tmp_path: Path):
    """The suggested_old span must really exist in the fixture draft."""
    adapter = WordAdapter()
    paragraphs = adapter.list_paragraphs(adapter.open_bytes(sample_new_draft()))
    assert any("进行图像分类" in item.text for item in paragraphs)
