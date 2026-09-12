from __future__ import annotations

import json
from pathlib import Path

from tests.helpers import (
    OVERCLAIM_CLAIM_QUOTE,
    OVERCLAIM_EVIDENCE_QUOTE,
    sample_full_thesis_draft,
    sample_overclaim_draft,
    sample_supported_claim_draft,
)
from thesis_review.history.store import HistoryStore
from thesis_review.service import ThesisReviewService
from thesis_review.worker import MAX_CONTENT_FINDINGS, NAV_BUDGET, SEARCH_BUDGET, Worker
from thesis_review.word.adapter import WordAdapter


def _open(worker: Worker, data: bytes) -> None:
    import base64

    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(data).decode("ascii")})


def test_faux_agent_lists_outline_and_records_multiple_content_kinds(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    overclaim = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="overclaim",
        data=sample_overclaim_draft(),
        output_dir=tmp_path / "out-overclaim",
        use_pi=True,
        faux=True,
        faux_scenario="overclaim",
    )
    ops = [item["op"] for item in json.loads((tmp_path / "out-overclaim" / "overclaim-trace.json").read_text(encoding="utf-8"))["ops"]]
    assert "list_outline" in ops
    assert "read_section" in ops
    assert "record_argument_finding" in ops
    assert any(item.source == "argument" for item in overclaim.findings)

    mismatch = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="full",
        data=sample_full_thesis_draft(),
        output_dir=tmp_path / "out-full",
        use_pi=True,
        faux=True,
        faux_scenario="data_mismatch",
    )
    kinds = {item.subtype for item in mismatch.findings}
    assert "data_consistency" in kinds


def test_fake_quote_is_rejected_and_no_evidence_is_abandoned(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_overclaim_draft())
    try:
        worker.dispatch(
            "record_content_finding",
            {
                "kind": "content",
                "subtype": "argument",
                "quote": "本文准确率达到 99.9%。",
                "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
                "problem": "编造。",
                "rationale": "编造。",
            },
        )
        raise AssertionError("fake quote should fail")
    except Exception as exc:
        assert getattr(exc, "code", "") == "quote_not_in_draft"
    assert worker.findings == []
    assert worker.gate_rejects >= 1

    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    abandoned = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="supported",
        data=sample_supported_claim_draft(),
        output_dir=tmp_path / "out-supported",
        use_pi=True,
        faux=True,
        faux_scenario="abandon",
    )
    assert all(item.source != "argument" for item in abandoned.findings)


def test_budgets_are_enforced(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_overclaim_draft())
    for _unused in range(NAV_BUDGET):
        worker.dispatch("list_outline", {})
    try:
        worker.dispatch("list_outline", {})
        raise AssertionError("nav budget")
    except Exception as exc:
        assert getattr(exc, "code", "") == "nav_budget"
    assert SEARCH_BUDGET >= 1
    assert MAX_CONTENT_FINDINGS >= 3
    assert OVERCLAIM_CLAIM_QUOTE


def test_worker_records_method_and_experiment_findings(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_overclaim_draft())
    outline = worker.dispatch("list_outline", {})["outline"]

    def _read(keyword: str) -> None:
        ordinal = next(item["ordinal"] for item in outline if keyword in item["text"])
        worker.dispatch("read_section", {"start_ordinal": ordinal})

    # data/method/experiment findings require their sections to have been read.
    _read("2 方法")
    _read("4 结论")
    method = worker.dispatch(
        "record_content_finding",
        {
            "kind": "content",
            "subtype": "method",
            "quote": "本文使用卷积神经网络提取图像特征并完成分类。",
            "evidence_quote": "准确率由 0.81 提高到 0.83。",
            "problem": "方法未说明训练轮次与数据划分，难以支持分类结论。",
            "rationale": "方法段只有网络类型，结果段给出准确率变化。",
            "draft_id": "overclaim",
        },
    )
    experiment = worker.dispatch(
        "record_content_finding",
        {
            "kind": "content",
            "subtype": "experiment",
            "quote": OVERCLAIM_CLAIM_QUOTE,
            "evidence_quote": OVERCLAIM_EVIDENCE_QUOTE,
            "problem": "比较结论缺少充分实验与显著性检验。",
            "rationale": "未见基线对照与检验。",
            "draft_id": "overclaim",
        },
    )
    assert method["ok"] and experiment["ok"]
    subtypes = {item.subtype for item in worker.findings}
    assert "method" in subtypes and "experiment" in subtypes


def test_web_skip_faux_does_not_search(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="full",
        data=sample_full_thesis_draft(),
        output_dir=tmp_path / "out",
        use_pi=True,
        faux=True,
        faux_scenario="web_skip",
        offline_fallback=False,
    )
    ops = [item["op"] for item in json.loads((tmp_path / "out" / "full-trace.json").read_text(encoding="utf-8"))["ops"]]
    assert "web_search" not in ops
    assert "find_text" in ops
    assert result.reviewed_path.is_file()


def test_web_fail_faux_does_not_fabricate_external_finding(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="full",
        data=sample_full_thesis_draft(),
        output_dir=tmp_path / "out",
        use_pi=True,
        faux=True,
        faux_scenario="web_fail",
        offline_fallback=False,
    )
    ops = [item["op"] for item in json.loads((tmp_path / "out" / "full-trace.json").read_text(encoding="utf-8"))["ops"]]
    assert "web_search" in ops
    assert "record_external_finding" not in ops
    assert all(item.kind != "external" for item in result.findings)
    comments = WordAdapter().extract_comments(WordAdapter().open_path(result.reviewed_path))
    assert comments == []


def test_web_needed_records_external_only_with_real_sources(tmp_path: Path):
    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="full",
        data=sample_full_thesis_draft(),
        output_dir=tmp_path / "out",
        use_pi=True,
        faux=True,
        faux_scenario="web_needed",
        offline_fallback=False,
    )
    ops = [item["op"] for item in json.loads((tmp_path / "out" / "full-trace.json").read_text(encoding="utf-8"))["ops"]]
    assert "web_search" in ops
    externals = [item for item in result.findings if item.kind == "external" or item.source == "external"]
    if "record_external_finding" in ops:
        assert externals
        assert all(item.external_sources and item.external_sources[0].url for item in externals)
    else:
        assert externals == []
    comments = WordAdapter().extract_comments(WordAdapter().open_path(result.reviewed_path))
    assert comments == []


def test_repeated_faux_review_ignores_stale_worker_port(tmp_path: Path):
    import os
    import socket
    import time

    service = ThesisReviewService(
        store=HistoryStore(tmp_path / "history.sqlite"),
        adapter=WordAdapter(),
        home=tmp_path,
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    leftover = output_dir / "worker.port"
    leftover.write_text(str(dead_port), encoding="ascii")
    past = time.time() - 120
    os.utime(leftover, (past, past))

    first = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="overclaim",
        data=sample_overclaim_draft(),
        output_dir=output_dir,
        use_pi=True,
        faux=True,
        faux_scenario="skip_history",
        offline_fallback=False,
    )
    assert not first.warning
    second = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="overclaim-2",
        data=sample_overclaim_draft(),
        output_dir=output_dir,
        use_pi=True,
        faux=True,
        faux_scenario="skip_history",
        offline_fallback=False,
    )
    assert not second.warning
    assert first.findings
    assert second.findings
