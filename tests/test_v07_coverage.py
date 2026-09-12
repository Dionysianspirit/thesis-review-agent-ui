"""V0.7 chapter coverage: reading the draft must be observable per section."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from tests.helpers import sample_full_thesis_draft
from thesis_review.live import read_progress
from thesis_review.worker import Worker


def _open(worker: Worker, data: bytes) -> None:
    worker.dispatch("open_draft", {"bytes_b64": base64.b64encode(data).decode("ascii")})


def _read_section(worker: Worker, keyword: str) -> dict:
    outline = worker.dispatch("list_outline", {})["outline"]
    heading = next(item for item in outline if keyword in item["text"])
    return worker.dispatch("read_section", {"start_ordinal": heading["ordinal"]})


def test_open_draft_builds_section_map_with_front_matter(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    status = worker.dispatch("coverage_status", {})
    titles = [item["title"] for item in status["sections"]]
    assert titles[0] == "开篇"
    assert any("摘要" in title for title in titles)
    assert any("3 实验" in title for title in titles)
    assert status["covered"] == 0
    assert status["uncovered"] == titles
    assert all(item["status"] == "unread" for item in status["sections"])
    assert all(item["n_paras"] >= 1 for item in status["sections"])


def test_read_marks_only_covered_sections_and_find_probe_counts_partially(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    _read_section(worker, "摘要")
    worker.dispatch("find_text", {"needle": "81%"})
    status = worker.dispatch("coverage_status", {})
    by_title = {item["title"]: item for item in status["sections"]}
    # Reading the 摘要 section covers exactly that section span, nothing else.
    assert next(status for title, status in by_title.items() if "摘要" in title)["status"] == "read"
    assert by_title["开篇"]["status"] == "unread"
    assert by_title["1 引言"]["status"] == "unread"
    probed = [item for item in status["sections"] if item["status"] == "probed"]
    assert probed, "a find_text hit should mark its section probed, not read"
    assert status["covered"] < status["total"]


def test_coverage_hint_prioritizes_uncovered_sections(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    _read_section(worker, "摘要")
    status = worker.dispatch("coverage_status", {})
    assert "优先补齐" in status["hint"]
    assert "未覆盖" not in status["hint"] or status["uncovered"]
    # Reading everything clears the hint.
    for section in list(worker.sections):
        worker.dispatch("read_section", {"start_ordinal": section["ordinal"], "limit": 8})
    final = worker.dispatch("coverage_status", {})
    assert final["hint"] == ""
    assert final["covered"] == final["total"]
    assert final["uncovered"] == []


def test_nav_budget_exhaustion_names_uncovered_sections(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    _open(worker, sample_full_thesis_draft())
    for _unused in range(worker.nav_budget):
        worker.dispatch("list_outline", {})
    try:
        worker.dispatch("list_outline", {})
        raise AssertionError("nav budget should be exhausted")
    except Exception as exc:
        assert getattr(exc, "code", "") == "nav_budget"
        assert "未覆盖章节" in str(exc)


def test_commit_writes_coverage_into_trace_live_and_result(tmp_path: Path):
    worker = Worker(home=tmp_path, teacher_id="teacher-a", student_id="zhou", major="人工智能")
    live = tmp_path / "live"
    worker.live = live
    _open(worker, sample_full_thesis_draft())
    _read_section(worker, "摘要")
    result = worker.dispatch("commit_review", {"draft_id": "full", "output_dir": str(tmp_path / "out")})
    assert result["coverage"]["total"] > 0
    assert result["coverage"]["covered"] >= 1
    trace = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
    assert trace["coverage"]["total"] == result["coverage"]["total"]
    assert trace["coverage"]["uncovered"]
    progress = read_progress(live)
    coverage_events = [item for item in progress["tech_log"] if item["op"] == "coverage"]
    assert coverage_events
    assert "章节覆盖" in coverage_events[-1]["intent"]


def test_faux_review_session_quality_reports_chapter_coverage(tmp_path: Path):
    from thesis_review.history.store import HistoryStore
    from thesis_review.service import ThesisReviewService
    from thesis_review.word.adapter import WordAdapter

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
        faux_scenario="overclaim",
        offline_fallback=False,
    )
    coverage = result.quality.get("chapter_coverage") or {}
    assert coverage.get("total", 0) > 0
    session = service.sessions.get(result.session_id)
    assert session.quality.get("chapter_coverage", {}).get("total") == coverage["total"]
    # The overclaim faux scenario reads 结论 and 实验结果 but never the whole
    # thesis: coverage must be honest about what was left unread.
    assert coverage.get("covered", 0) < coverage["total"]


def test_offline_rule_run_has_no_chapter_coverage(tmp_path: Path):
    from thesis_review.history.store import HistoryStore
    from thesis_review.service import ThesisReviewService
    from thesis_review.word.adapter import WordAdapter

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
        offline_fallback=False,
    )
    assert "chapter_coverage" not in result.quality
