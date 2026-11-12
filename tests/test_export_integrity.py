"""Export integrity: what the teacher approved is what reaches the Word file.

The formal export must never drop an approved comment or revision silently.
Either the write lands, or the result names the finding and the reason so the
teacher can add it by hand. The Teacher Gate itself is unchanged: pending and
rejected findings still never reach the formal document.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from docx import Document

from thesis_review.checks.format import check_format
from thesis_review.comments import ASSISTANT_AUTHOR
from thesis_review.errors import ReviewError
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.types import (
    DECISION_ACCEPTED,
    DECISION_PENDING,
    DECISION_REJECTED,
    Finding,
    prepare_candidate,
)
from thesis_review.word.adapter import WordAdapter

sys.modules.setdefault("webview", MagicMock())

from thesis_review.gui.app import Bridge  # noqa: E402


def _docx(build) -> bytes:
    doc = Document()
    build(doc)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _service(tmp_path: Path) -> ThesisReviewService:
    return ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )


def _review(service: ThesisReviewService, tmp_path: Path, data: bytes):
    return service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=data,
        output_dir=tmp_path / "out",
        use_model=False,
    )


def _accept_all(service: ThesisReviewService, session_id: str) -> None:
    for item in service.sessions.get(session_id).findings:
        service.decide_finding(session_id=session_id, finding_id=item.id, decision=DECISION_ACCEPTED)


def _word(path: str):
    adapter = WordAdapter()
    opened = adapter.open_path(path)
    return adapter, opened


def _deleted(path: str) -> list[str]:
    adapter, opened = _word(path)
    return [rev.text for rev in adapter.extract_revisions(opened) if rev.kind == "del"]


def _accepted_text(path: str) -> str:
    """Body text as it reads once every tracked revision is accepted."""
    adapter, opened = _word(path)
    return "\n".join(item.text for item in adapter.list_paragraphs(opened))


def _manual_session(service: ThesisReviewService, tmp_path: Path, data: bytes, findings: list[Finding]):
    paper = tmp_path / "new.docx"
    paper.write_bytes(data)
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    session = service.start_session(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path=str(paper),
        output_dir=str(out),
    )
    session.source_path = str(paper)
    session.findings = findings
    service.sessions.save(session)
    return session


def _finding(finding_id: str, anchor: str, **extra) -> Finding:
    base = dict(
        id=finding_id,
        category="A",
        source="rule",
        code="padded_adverb",
        problem=f"问题 {finding_id}：叠用程度副词。",
        rationale="应改为可核验的表述。",
        quote="非常非常",
        anchor=anchor,
        paragraph_index=2,
        apply="both",
        kind="language",
        suggested_old="非常非常",
        suggested_new="较为",
        teacher_decision=DECISION_ACCEPTED,
    )
    base.update(extra)
    return prepare_candidate(Finding(**base))


def _two_adverbs(doc) -> None:
    doc.add_paragraph("本科毕业论文")
    doc.add_paragraph("该方法非常非常有效，而且特别特别稳定。")


def _repeated_adverb(doc) -> None:
    doc.add_paragraph("本科毕业论文")
    doc.add_paragraph("该方法非常非常有效，结果也非常非常稳定。")


def _anchor_of(data: bytes, needle: str) -> str:
    adapter = WordAdapter()
    paragraphs = adapter.list_paragraphs(adapter.open_bytes(data))
    return next(item.anchor for item in paragraphs if needle in item.text)


# --- writes that used to vanish ------------------------------------------------


def test_two_approved_revisions_in_one_paragraph_are_both_written(tmp_path: Path):
    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(_two_adverbs))
    revisions = [item for item in result.findings if item.suggested_old]
    assert {item.suggested_old for item in revisions} == {"非常非常", "特别特别"}
    assert len({item.anchor for item in revisions}) == 1, "both findings sit on one paragraph"
    _accept_all(service, result.session_id)

    exported = service.export_final(session_id=result.session_id)

    assert exported["ok"] is True
    assert sorted(_deleted(exported["reviewed_path"])) == ["特别特别", "非常非常"]
    assert exported["not_written"] == []
    assert exported["n_exported"] == exported["n_approved"] == len(result.findings)


def test_three_revisions_chain_through_the_refreshed_anchor(tmp_path: Path):
    def build(doc) -> None:
        doc.add_paragraph("本科毕业论文")
        doc.add_paragraph("该方法非常非常有效，特别特别稳定，一定一定可靠。")

    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(build))
    _accept_all(service, result.session_id)
    exported = service.export_final(session_id=result.session_id)
    assert sorted(_deleted(exported["reviewed_path"])) == ["一定一定", "特别特别", "非常非常"]
    assert exported["not_written"] == []


def test_phrase_repeated_in_one_paragraph_is_revised_everywhere(tmp_path: Path):
    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(_repeated_adverb))
    _accept_all(service, result.session_id)

    exported = service.export_final(session_id=result.session_id)

    assert _deleted(exported["reviewed_path"]) == ["非常非常", "非常非常"]
    assert "非常非常" not in _accepted_text(exported["reviewed_path"])
    assert exported["not_written"] == []
    assert exported["n_exported"] == exported["n_approved"]


def test_table_before_any_paragraph_gets_its_comment_on_the_following_paragraph(tmp_path: Path):
    def build(doc) -> None:
        table = doc.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "模型"
        table.cell(0, 1).text = "准确率"
        doc.add_paragraph("正文第一段，说明研究背景。")

    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(build))
    caption = next(item for item in result.findings if item.code == "missing_table_caption")
    assert caption.anchor.startswith("P"), "a comment can only hang on a body paragraph"
    _accept_all(service, result.session_id)

    exported = service.export_final(session_id=result.session_id)

    adapter, opened = _word(exported["reviewed_path"])
    bodies = [item.text for item in adapter.extract_comments(opened)]
    caption_body = next(body for body in bodies if "题注" in body)
    assert "上方的表格" in caption_body, "the comment must say which table it is about"
    assert len(bodies) == exported["n_approved"] == exported["n_exported"]
    assert exported["not_written"] == []


def test_table_finding_is_located_by_paragraph_not_by_table_number(tmp_path: Path):
    def build(doc) -> None:
        for index in range(1, 6):
            doc.add_paragraph(f"第 {index} 段正文。")
        doc.add_paragraph("此段在表格之前，但不是题注。")
        doc.add_table(rows=1, cols=1)

    adapter = WordAdapter()
    opened = adapter.open_bytes(_docx(build))
    paragraphs = adapter.list_paragraphs(opened)
    findings = check_format(paragraphs, adapter.list_tables(opened))
    caption = next(item for item in findings if item.code == "missing_table_caption")
    before_table = next(item for item in paragraphs if "表格之前" in item.text)
    # It used to carry the table's own ordinal (1), which reads as "第 1 段"
    # and puts the finding in the wrong chapter of the eval tables.
    assert caption.paragraph_index == before_table.ordinal == 6
    assert caption.anchor == before_table.anchor


# --- writes that cannot land must be reported, never counted --------------------


def test_stale_anchor_is_reported_and_not_counted_as_written(tmp_path: Path):
    data = _docx(_two_adverbs)
    service = _service(tmp_path)
    real = _anchor_of(data, "非常非常")
    stale = real.split("#")[0] + "#0000"
    session = _manual_session(service, tmp_path, data, [_finding("f-stale", stale)])

    exported = service.export_final(session_id=session.id)

    assert exported["ok"] is True
    assert exported["n_approved"] == 1
    assert exported["n_exported"] == 0
    assert [item["id"] for item in exported["not_written"]] == ["f-stale"]
    entry = exported["not_written"][0]
    assert entry["comment_written"] is False and entry["revision_written"] is False
    assert set(entry["missing"]) == {"comment", "revision"}
    assert entry["reason"], "the teacher needs to know why"
    assert "问题 f-stale" in entry["problem"]
    adapter, opened = _word(exported["reviewed_path"])
    assert adapter.extract_comments(opened) == []
    assert adapter.extract_revisions(opened) == []
    assert "未能" in exported["warning"]


def test_anchor_is_only_refreshed_for_paragraphs_this_export_changed(tmp_path: Path):
    """A stale anchor on an untouched paragraph must stay a refusal, not be
    quietly re-pointed at whatever now sits at that position."""

    def build(doc) -> None:
        doc.add_paragraph("本科毕业论文")
        doc.add_paragraph("该方法非常非常有效。")
        doc.add_paragraph("结果也非常非常稳定。")

    data = _docx(build)
    service = _service(tmp_path)
    good = _anchor_of(data, "该方法")
    other = _anchor_of(data, "结果也")
    stale_other = other.split("#")[0] + "#ffff"
    session = _manual_session(
        service,
        tmp_path,
        data,
        [_finding("f-good", good), _finding("f-stale", stale_other, paragraph_index=3)],
    )

    exported = service.export_final(session_id=session.id)

    assert exported["n_exported"] == 1
    assert [item["id"] for item in exported["not_written"]] == ["f-stale"]
    assert _deleted(exported["reviewed_path"]) == ["非常非常"]
    assert "结果也非常非常稳定。" in _accepted_text(exported["reviewed_path"])


def test_comment_written_but_revision_missing_is_reported_as_partial(tmp_path: Path):
    data = _docx(_two_adverbs)
    service = _service(tmp_path)
    finding = _finding(
        "f-partial",
        _anchor_of(data, "非常非常"),
        suggested_old="这段文字并不在稿件里",
        suggested_new="替换文本",
    )
    session = _manual_session(service, tmp_path, data, [finding])

    exported = service.export_final(session_id=session.id)

    assert exported["n_approved"] == 1
    assert exported["n_exported"] == 0, "a half-written finding is not a written finding"
    entry = exported["not_written"][0]
    assert entry["comment_written"] is True
    assert entry["revision_written"] is False
    assert entry["missing"] == ["revision"]
    adapter, opened = _word(exported["reviewed_path"])
    assert len(adapter.extract_comments(opened)) == 1
    assert adapter.extract_revisions(opened) == []


def test_document_without_body_paragraph_reports_every_unwritable_finding(tmp_path: Path):
    def build(doc) -> None:
        doc.add_table(rows=1, cols=1)

    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(build))
    assert result.findings
    _accept_all(service, result.session_id)

    exported = service.export_final(session_id=result.session_id)

    assert exported["ok"] is True
    assert exported["n_exported"] == 0
    assert len(exported["not_written"]) == exported["n_approved"] == len(result.findings)
    assert all(item["reason"] for item in exported["not_written"])


def test_clean_export_reports_nothing_missing(tmp_path: Path):
    from tests.helpers import sample_new_draft

    service = _service(tmp_path)
    result = _review(service, tmp_path, sample_new_draft())
    _accept_all(service, result.session_id)
    exported = service.export_final(session_id=result.session_id)
    assert exported["not_written"] == []
    assert exported["n_not_written"] == 0
    assert exported["n_exported"] == exported["n_approved"] == len(result.findings)
    assert exported["warning"] == ""
    adapter, opened = _word(exported["reviewed_path"])
    assert len(adapter.extract_comments(opened)) == len(result.findings)


# --- the Teacher Gate is untouched ---------------------------------------------


def test_rejected_and_pending_neighbours_on_the_same_paragraph_stay_out(tmp_path: Path):
    def build(doc) -> None:
        doc.add_paragraph("本科毕业论文")
        doc.add_paragraph("该方法非常非常有效，特别特别稳定，一定一定可靠。")

    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(build))
    by_old = {item.suggested_old: item for item in result.findings if item.suggested_old}
    service.decide_finding(
        session_id=result.session_id, finding_id=by_old["非常非常"].id, decision=DECISION_ACCEPTED
    )
    service.decide_finding(
        session_id=result.session_id, finding_id=by_old["特别特别"].id, decision=DECISION_REJECTED
    )
    assert by_old["一定一定"].teacher_decision == DECISION_PENDING

    exported = service.export_final(session_id=result.session_id, allow_pending=True)

    assert _deleted(exported["reviewed_path"]) == ["非常非常"]
    text = _accepted_text(exported["reviewed_path"])
    assert "特别特别稳定" in text and "一定一定可靠" in text
    adapter, opened = _word(exported["reviewed_path"])
    assert len(adapter.extract_comments(opened)) == 1
    assert exported["n_approved"] == exported["n_exported"] == 1
    assert exported["not_written"] == []


def test_every_assistant_write_is_attributed_to_the_assistant(tmp_path: Path):
    service = _service(tmp_path)
    result = _review(service, tmp_path, _docx(_repeated_adverb))
    _accept_all(service, result.session_id)
    exported = service.export_final(session_id=result.session_id)
    adapter, opened = _word(exported["reviewed_path"])
    assert {rev.author for rev in adapter.extract_revisions(opened)} == {ASSISTANT_AUTHOR}


# --- adapter ---------------------------------------------------------------------


def test_adapter_replace_all_stays_inside_the_anchored_paragraph():
    def build(doc) -> None:
        doc.add_paragraph("该方法非常非常有效，结果也非常非常稳定。")
        doc.add_paragraph("另一段也写了非常非常好。")

    adapter = WordAdapter()
    opened = adapter.open_bytes(_docx(build))
    first = adapter.list_paragraphs(opened)[0]

    with pytest.raises(ReviewError) as caught:
        adapter.replace_tracked(opened, anchor=first.anchor, old="非常非常", new="较为", author="审改助手")
    assert caught.value.code == "ambiguous_target", "single replace keeps refusing ambiguity"

    result = adapter.replace_tracked(
        opened, anchor=first.anchor, old="非常非常", new="较为", author="审改助手", replace_all=True
    )
    assert result.n_replaced == 2
    assert result.new_anchor and result.new_anchor != first.anchor
    assert result.new_text == "该方法较为有效，结果也较为稳定。"
    assert adapter.list_paragraphs(opened)[1].text == "另一段也写了非常非常好。"


def test_adapter_replace_all_refuses_when_nothing_matches():
    adapter = WordAdapter()
    opened = adapter.open_bytes(_docx(_two_adverbs))
    target = adapter.list_paragraphs(opened)[1]
    before = adapter.export_bytes(opened)
    with pytest.raises(ReviewError) as caught:
        adapter.replace_tracked(
            opened, anchor=target.anchor, old="不存在的片段", new="x", author="审改助手", replace_all=True
        )
    assert caught.value.code == "not_found"
    assert adapter.export_bytes(opened) == before


# --- session data survives the export ---------------------------------------------


def test_export_keeps_review_duration_and_recall_snapshot(tmp_path: Path):
    from tests.helpers import sample_new_draft

    service = _service(tmp_path)
    result = _review(service, tmp_path, sample_new_draft())
    session = service.sessions.get(result.session_id)
    session.quality = dict(session.quality, duration_s=37.5, recall={"confirmed": 2, "recalled": 1})
    service.sessions.save(session)
    _accept_all(service, result.session_id)

    service.export_final(session_id=result.session_id)

    quality = service.sessions.get(result.session_id).quality
    assert quality["duration_s"] == 37.5, "generating the Word file must not zero the review time"
    assert quality["recall"] == {"confirmed": 2, "recalled": 1}
    assert quality["accepted"] == len(result.findings)


def test_export_report_is_stored_with_the_session(tmp_path: Path):
    data = _docx(_two_adverbs)
    service = _service(tmp_path)
    stale = _anchor_of(data, "非常非常").split("#")[0] + "#0000"
    session = _manual_session(service, tmp_path, data, [_finding("f-stale", stale)])
    service.export_final(session_id=session.id)
    stored = service.sessions.get(session.id).quality["export"]
    assert stored["n_approved"] == 1 and stored["n_exported"] == 0
    assert stored["not_written"][0]["id"] == "f-stale"


# --- teacher-facing surface ----------------------------------------------------------


def _bridge_with(tmp_path: Path, data: bytes, findings: list[Finding]) -> Bridge:
    bridge = Bridge(tmp_path)
    session = _manual_session(bridge.service, tmp_path, data, findings)
    bridge.session = bridge.service.sessions.get(session.id)
    bridge.findings = [item.to_dict() for item in bridge.session.findings]
    return bridge


def test_bridge_tells_the_teacher_which_approved_items_were_not_written(tmp_path: Path):
    data = _docx(_two_adverbs)
    good = _anchor_of(data, "非常非常")
    stale = good.split("#")[0] + "#0000"
    bridge = _bridge_with(
        tmp_path,
        data,
        [_finding("f-good", good), _finding("f-stale", stale, suggested_old="特别特别")],
    )

    result = bridge.export_final(False)

    assert result["ok"] is True
    assert result["n_exported"] == 1 and result["n_approved"] == 2
    assert "写入 1 条" in bridge.status
    assert "1 条" in bridge.status and "未能" in bridge.status
    state = bridge.state()
    report = state["export_report"]
    assert report["n_exported"] == 1
    assert [item["id"] for item in report["not_written"]] == ["f-stale"]


def test_export_report_survives_restart_and_clears_on_a_clean_reexport(tmp_path: Path):
    data = _docx(_two_adverbs)
    stale = _anchor_of(data, "非常非常").split("#")[0] + "#0000"
    bridge = _bridge_with(tmp_path, data, [_finding("f-stale", stale)])
    bridge.export_final(False)

    reopened = Bridge(tmp_path)
    assert reopened.state()["export_report"]["not_written"][0]["id"] == "f-stale"

    # The teacher rejects the unwritable item and exports again: nothing is missing now.
    reopened.decide_finding("f-stale", DECISION_REJECTED)
    reopened.export_final(False)
    assert reopened.state()["export_report"]["not_written"] == []


def test_bridge_reports_an_export_that_could_not_run_instead_of_raising(tmp_path: Path):
    """A missing source file used to surface as an unhandled bridge error: the
    button did nothing and the teacher saw no reason."""
    data = _docx(_two_adverbs)
    bridge = _bridge_with(tmp_path, data, [_finding("f-good", _anchor_of(data, "非常非常"))])
    Path(bridge.session.source_path).unlink()

    result = bridge.export_final(False)

    assert result["ok"] is False
    assert "找不到本稿文件" in result["message"]
    assert bridge.state()["export_report"] is None


def test_state_without_an_export_has_no_report(tmp_path: Path):
    bridge = _bridge_with(tmp_path, _docx(_two_adverbs), [])
    assert bridge.state()["export_report"] is None


def test_ui_surfaces_unwritten_items():
    from thesis_review.paths import gui_dir

    html = (gui_dir() / "ui.html").read_text(encoding="utf-8")
    js = (gui_dir() / "ui.js").read_text(encoding="utf-8")
    assert 'id="export-unwritten"' in html
    assert "renderExportReport" in js
    assert "not_written" in js
    assert "手动" in js, "the teacher is told to add the missing items by hand"


def test_demo_gate_requires_every_approved_item_to_be_written(tmp_path: Path):
    from thesis_review.cli import build_service
    from thesis_review.demo import run_teacher_demo

    payload = run_teacher_demo(build_service(tmp_path / "home"), tmp_path / "out")
    assert payload["ok"] is True
    assert payload["not_written"] == []
    assert payload["n_exported"] == payload["n_approved"] >= 1
