"""V0.7 real-model quality gates at the worker record boundary."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.helpers import (
    OVERCLAIM_CLAIM_QUOTE,
    OVERCLAIM_EVIDENCE_QUOTE,
    sample_full_thesis_draft,
    sample_overclaim_draft,
)
from thesis_review.errors import ReviewError
from thesis_review.worker import Worker


def _open(worker: Worker, data: bytes) -> None:
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(data).decode("ascii")})


def _outline_ordinal(worker: Worker, keyword: str) -> int:
    outline = worker.dispatch("list_outline", {})["outline"]
    return next(item["ordinal"] for item in outline if keyword in item["text"])


def test_record_rejects_placeholder_problem_text(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_overclaim_draft())
    with pytest.raises(ReviewError) as caught:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "argument",
                "quote": OVERCLAIM_CLAIM_QUOTE,
                "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
                "problem": "有问题。",
                "rationale": "证据不足以支撑该结论。",
            },
        )
    assert caught.value.code == "invalid_params"
    assert "空泛" in str(caught.value)
    with pytest.raises(ReviewError) as missing:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "argument",
                "quote": OVERCLAIM_CLAIM_QUOTE,
                "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
                "problem": "",
                "rationale": "证据不足以支撑该结论。",
            },
        )
    assert missing.value.code == "invalid_params"
    assert worker.findings == []


def test_record_rejects_copied_rationale(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_overclaim_draft())
    with pytest.raises(ReviewError) as caught:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "argument",
                "quote": OVERCLAIM_CLAIM_QUOTE,
                "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
                "problem": "结论缺少显著性检验且用词过满。",
                "rationale": "结论缺少显著性检验且用词过满。",
            },
        )
    assert caught.value.code == "invalid_params"
    with pytest.raises(ReviewError) as copied_quote:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "argument",
                "quote": OVERCLAIM_CLAIM_QUOTE,
                "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
                "problem": OVERCLAIM_CLAIM_QUOTE,
                "rationale": "结论缺少显著性检验且用词过满。",
            },
        )
    assert copied_quote.value.code == "invalid_params"


def test_high_risk_finding_requires_actually_read_section(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    # find_text alone (a 40-char snippet) is not enough context for a
    # data_consistency conclusion about the section it hits.
    worker.dispatch("find_text", {"needle": "81%"})
    with pytest.raises(ReviewError) as caught:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "data_consistency",
                "quote": "准确率为 81%",
                "evidence_quote": "准确率为 85%",
                "problem": "摘要与结论中的准确率数值不一致。",
                "rationale": "同一指标在不同章节给出了不同数值。",
            },
        )
    assert caught.value.code == "section_unread"
    assert worker.findings == []
    # After reading the sections holding both sides of the mismatch the same
    # call is accepted (the claim quote resolves to its first occurrence, in
    # the abstract).
    worker.dispatch("read_section", {"start_ordinal": _outline_ordinal(worker, "摘要")})
    worker.dispatch("read_section", {"start_ordinal": _outline_ordinal(worker, "4 实验结果")})
    accepted = worker.dispatch(
        "record_content_finding",
        {
            "kind": "content",
            "subtype": "data_consistency",
            "quote": "准确率为 81%",
            "evidence_quote": "准确率为 85%",
            "problem": "摘要与结论中的准确率数值不一致。",
            "rationale": "同一指标在不同章节给出了不同数值。",
        },
    )
    assert accepted["ok"] is True


def test_low_risk_finding_does_not_require_read_section(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    result = worker.dispatch(
        "record_content_finding",
        {
            "kind": "content",
            "subtype": "structure",
            "quote": OVERCLAIM_CLAIM_QUOTE,
            "problem": "章节结构存在明显的断裂需要老师核对。",
            "rationale": "该主张脱离所在章节的论证主线。",
        },
    )
    assert result["ok"] is True


def test_truncated_reads_expose_continuation(tmp_path: Path):
    from tests.helpers import sample_long_section_draft

    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_long_section_draft())
    result = worker.dispatch("read_paragraphs", {"start_ordinal": 2, "limit": 8})
    assert result["truncated"] is True
    assert result["next_ordinal"] == result["paragraphs"][-1]["ordinal"] + 1
    assert result["remaining_paras"] >= 1
    resumed = worker.dispatch("read_paragraphs", {"start_ordinal": result["next_ordinal"], "limit": 8})
    assert resumed["paragraphs"][0]["ordinal"] == result["next_ordinal"]
