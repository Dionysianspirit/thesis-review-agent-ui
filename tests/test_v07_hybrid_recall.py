"""V0.7 hybrid history recall: anchors close the long-paragraph dilution gap.

Similarity still never promotes itself to a recidivism verdict.
"""
from __future__ import annotations

import base64
from pathlib import Path

from tests.helpers import sample_history_v1
from thesis_review.history.semantic import DEFAULT_THRESHOLD, hybrid_recall, semantic_recall
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.types import IssueRecord, ParagraphView
from thesis_review.word.adapter import WordAdapter


def _issue(span: str, text: str = "", problem: str = "") -> IssueRecord:
    return IssueRecord(
        id="issue-1",
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        source_draft_id="v1",
        category="B",
        status="confirmed",
        original_kind="comment",
        original_text=text or f"旧稿批注：{span}",
        original_span=span,
        original_context="",
        original_anchor="",
        problem=problem,
    )


def _paras(*texts: str) -> list[ParagraphView]:
    return [ParagraphView(ordinal=index + 1, anchor=f"P{index + 1}", text=text) for index, text in enumerate(texts)]


LONG_REWRITE = (
    "本章在第 3.2 节给出的数据集上重新训练并调参，加入了数据增强与早停策略，"
    "在相同划分下重复了五次实验并取平均，最终在测试集上得到的准确率为 81%，"
    "与前期探索性结果保持一致，且训练曲线已经收敛，无明显过拟合迹象。"
)


def test_long_paragraph_dilution_is_quantified_and_fixed_by_anchors():
    issue = _issue("对比实验中准确率为 81%，需补充基线")
    paras = _paras(LONG_REWRITE)
    ngram = semantic_recall([issue], paras, threshold=DEFAULT_THRESHOLD)
    hybrid = hybrid_recall([issue], paras, threshold=DEFAULT_THRESHOLD)
    # The n-gram path genuinely misses this rewrite: that is the measured gap.
    assert ngram == []
    assert hybrid, "anchor blending should recall the paraphrased 81% paragraph"
    assert hybrid[0].method == "hybrid"
    assert hybrid[0].paragraph_index == 1


def test_anchor_hits_alone_never_clear_threshold():
    # No textual agreement with the issue besides one shared number: the blend
    # must stay below threshold, keeping precision on the recall side.
    issue = _issue("对比实验中准确率为 81%，需补充基线")
    unrelated = _paras("文献综述部分引用了 2021 年之后发表的 81 篇中外文献，覆盖面较广。")
    assert hybrid_recall([issue], unrelated, threshold=DEFAULT_THRESHOLD) == []


def test_hybrid_keeps_plain_ngram_behavior_when_no_anchors():
    issue = _issue("本研究非常非常有效。")
    paras = _paras("实验段落中再次出现了非常非常有效的表述。")
    hits = hybrid_recall([issue], paras)
    assert hits and hits[0].method == "ngram"


def test_worker_hybrid_candidates_stay_recall_only(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    service.ingest_history(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="v1",
        data=sample_history_v1(),
    )
    for issue in service.store.list_issues(teacher_id="teacher-a", student_id="zhou"):
        service.confirm_issue(teacher_id="teacher-a", student_id="zhou", issue_id=issue.id)

    worker = __import__("thesis_review.worker", fromlist=["Worker"]).Worker(
        home=tmp_path,
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
    )
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(sample_history_v1()).decode("ascii")})
    result = worker.dispatch("semantic_history_candidates", {})
    for candidate in result["candidates"]:
        assert candidate["needs_context_check"] is True
        assert candidate["auto_recidivism"] is False
        assert candidate["method"] in {"ngram", "hybrid"}
    # Recall alone wrote nothing: findings stay empty until confirm_history_finding.
    assert worker.findings == []


def test_confirm_still_rejects_quote_outside_recall_location(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    candidates = service.ingest_history(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="v1",
        data=sample_history_v1(),
    )
    subjective = next(item for item in candidates if "主观评价" in item.original_text)
    service.confirm_issue(teacher_id="teacher-a", student_id="zhou", issue_id=subjective.id)

    worker = __import__("thesis_review.worker", fromlist=["Worker"]).Worker(
        home=tmp_path,
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
    )
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(sample_history_v1()).decode("ascii")})
    try:
        worker.dispatch(
            "confirm_history_finding",
            {
                "issue_id": subjective.id,
                "new_quote": "这段话不在召回位置附近也不在稿里出现的原句。",
                "rationale": "试图把相似直接升级为复犯。",
            },
        )
        raise AssertionError("invented quote must be rejected")
    except Exception as exc:
        assert getattr(exc, "code", "") == "quote_not_in_draft"
    assert worker.findings == []
