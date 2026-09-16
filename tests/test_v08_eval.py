"""V0.8 real-eval loop: metrics derived from session data, eval export."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from thesis_review.evalmetrics import compute_eval_metrics
from thesis_review.evalexport import (
    EVAL_JSON_NAME,
    EVAL_SCHEMA_VERSION,
    build_eval_payload,
    write_eval_export,
)
from thesis_review.session_store import ReviewSession
from thesis_review.types import Finding, MissedIssue


def _finding(
    *,
    id: str,
    kind: str,
    decision: str = "pending",
    subtype: str = "",
    paragraph_index: int = 0,
    rejection_reason: str = "",
) -> Finding:
    return Finding(
        id=id,
        category="B",
        source=kind,
        kind=kind,
        subtype=subtype,
        problem=f"问题 {id}",
        rationale="依据。",
        quote="原文",
        anchor=f"P{paragraph_index:04d}",
        paragraph_index=paragraph_index,
        apply="comment",
        teacher_decision=decision,
        rejection_reason=rejection_reason,
    )


TRACE = {
    "coverage": {
        "covered": 1,
        "total": 2,
        "sections": [
            {"ordinal": 1, "end": 5, "title": "1 引言", "n_paras": 4, "status": "read", "reads": 1},
            {"ordinal": 5, "end": 9, "title": "2 方法", "n_paras": 4, "status": "unread", "reads": 0},
        ],
    }
}


def test_metrics_counts_and_rate_denominator_excludes_pending():
    findings = [
        _finding(id="a", kind="format", decision="accepted"),
        _finding(id="b", kind="language", decision="edited_accepted"),
        _finding(id="c", kind="content", subtype="argument", decision="rejected", rejection_reason="false_positive"),
        _finding(id="d", kind="content", subtype="method", decision="pending"),
    ]
    metrics = compute_eval_metrics(findings)
    assert metrics["candidate_count"] == 4
    assert metrics["accepted_count"] == 1
    assert metrics["edited_accepted_count"] == 1
    assert metrics["rejected_count"] == 1
    assert metrics["pending_count"] == 1
    # Denominator is processed decisions only: pending never dilutes rates.
    assert metrics["processed_count"] == 3
    assert metrics["adopted_count"] == 2
    assert metrics["acceptance_rate"] == round(2 / 3, 4)
    assert metrics["direct_acceptance_rate"] == round(1 / 3, 4)
    assert metrics["edited_acceptance_rate"] == round(1 / 3, 4)
    assert metrics["rejection_rate"] == round(1 / 3, 4)


def test_metrics_all_pending_rates_are_unknown_not_zero():
    metrics = compute_eval_metrics([_finding(id="a", kind="format"), _finding(id="b", kind="language")])
    assert metrics["processed_count"] == 0
    assert metrics["acceptance_rate"] is None
    assert metrics["rejection_rate"] is None
    assert metrics["direct_acceptance_rate"] is None
    assert metrics["edited_acceptance_rate"] is None


def test_metrics_empty_session():
    metrics = compute_eval_metrics([])
    assert metrics["candidate_count"] == 0
    assert metrics["acceptance_rate"] is None
    assert metrics["missed_issue_count"] == 0


def test_category_breakdown_uses_existing_kind_subtype():
    findings = [
        _finding(id="a", kind="format", decision="accepted"),
        _finding(id="b", kind="content", subtype="argument", decision="accepted"),
        _finding(id="c", kind="content", subtype="argument", decision="rejected"),
        _finding(id="d", kind="content", subtype="method", decision="pending"),
    ]
    rows = {(row["category"], row["subtype"]): row for row in compute_eval_metrics(findings)["category_metrics"]}
    assert rows[("format", "")]["candidate_count"] == 1
    assert rows[("format", "")]["acceptance_rate"] == 1.0
    argument = rows[("content", "argument")]
    assert argument["candidate_count"] == 2
    assert argument["accepted"] == 1
    assert argument["rejected"] == 1
    # Category rate follows the same convention: processed denominator.
    assert argument["acceptance_rate"] == 0.5
    assert rows[("content", "method")]["acceptance_rate"] is None


def test_rejection_reasons_counter_includes_unclassified():
    findings = [
        _finding(id="a", kind="content", subtype="argument", decision="rejected", rejection_reason="duplicate"),
        _finding(id="b", kind="content", subtype="method", decision="rejected"),
    ]
    reasons = compute_eval_metrics(findings)["rejection_reasons"]
    assert reasons["duplicate"] == 1
    assert reasons["unclassified"] == 1


def test_chapter_observations_link_findings_and_missed_issues():
    findings = [
        _finding(id="a", kind="content", subtype="argument", decision="accepted", paragraph_index=2),
        _finding(id="b", kind="language", decision="rejected", paragraph_index=6),
    ]
    missed = [{"id": "m1", "section": "2 方法", "problem": "漏检"}]
    rows = compute_eval_metrics(findings, missed, TRACE)["chapter_observations"]
    by_chapter = {row["chapter"]: row for row in rows}
    assert by_chapter["1 引言"]["coverage_status"] == "read"
    assert by_chapter["1 引言"]["ai_candidates"] == 1
    assert by_chapter["1 引言"]["accepted"] == 1
    assert by_chapter["2 方法"]["coverage_status"] == "unread"
    assert by_chapter["2 方法"]["ai_candidates"] == 1
    assert by_chapter["2 方法"]["rejected"] == 1
    assert by_chapter["2 方法"]["teacher_missed_issues"] == 1


def test_unmatched_missed_issue_is_not_force_assigned():
    missed = [{"id": "m1", "section": "第 7 章附录之外", "problem": "漏检"}]
    rows = compute_eval_metrics([], missed, TRACE)["chapter_observations"]
    assert all(row["teacher_missed_issues"] == 0 for row in rows)


def test_coverage_summary_counts_read_probed_unread():
    trace = {
        "coverage": {
            "covered": 1,
            "total": 3,
            "sections": [
                {"ordinal": 1, "end": 5, "title": "A", "status": "read"},
                {"ordinal": 5, "end": 7, "title": "B", "status": "probed"},
                {"ordinal": 7, "end": 9, "title": "C", "status": "unread"},
            ],
        }
    }
    summary = compute_eval_metrics([], [], trace)["coverage_summary"]
    assert summary == {"read": 1, "probed": 1, "unread": 1, "total": 3}


def test_offline_run_without_trace_has_empty_chapters():
    metrics = compute_eval_metrics([_finding(id="a", kind="format", decision="accepted")], [], None)
    assert metrics["chapter_observations"] == []


def test_chapter_observations_tolerate_sections_without_end():
    trace = {
        "coverage": {
            "sections": [
                {"ordinal": 1, "title": "1 引言", "status": "read"},
                {"ordinal": 5, "end": 9, "title": "2 方法", "status": "unread"},
            ]
        }
    }
    findings = [_finding(id="a", kind="content", paragraph_index=6)]
    rows = compute_eval_metrics(findings, [], trace)["chapter_observations"]
    by_chapter = {row["chapter"]: row for row in rows}
    assert by_chapter["1 引言"]["ai_candidates"] == 0
    assert by_chapter["2 方法"]["ai_candidates"] == 1


def test_export_json_schema_is_stable():
    session = ReviewSession(
        id="s1",
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path="/home/teacher/Documents/毕业论文-小周.docx",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T01:00:00+00:00",
        model_snapshot={"provider": "openai-compatible", "model": "gpt-4o-mini", "reasoning": "medium"},
        findings=[_finding(id="a", kind="format", decision="accepted")],
        quality={"duration_s": 522.4},
        review_started_at="2026-09-13T00:10:00+00:00",
        review_completed_at="2026-09-13T00:18:42+00:00",
    )
    payload = build_eval_payload(session, TRACE)
    assert payload["schema_version"] == EVAL_SCHEMA_VERSION
    assert payload["session_id"] == "s1"
    assert set(payload) == {
        "schema_version",
        "session_id",
        "generated_at",
        "document",
        "participants",
        "model_snapshot",
        "timing",
        "token_usage",
        "chapter_coverage",
        "chapter_observations",
        "metrics",
        "category_metrics",
        "rejection_reasons",
        "findings",
        "teacher_missed_issues",
    }
    assert payload["document"] == {"draft_id": "new", "file_name": "毕业论文-小周.docx"}
    assert payload["timing"]["duration_s"] == 522.4
    finding = payload["findings"][0]
    assert finding["teacher_decision"] == "accepted"
    assert finding["kind"] == "format"
    assert payload["metrics"]["candidate_count"] == 1


def test_export_strips_secrets_and_full_paths():
    session = ReviewSession(
        id="s1",
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path="/home/teacher/Documents/毕业论文.docx",
        created_at="2026-09-13T00:00:00+00:00",
        updated_at="2026-09-13T01:00:00+00:00",
        # A leaked key must never survive into the export.
        model_snapshot={"provider": "p", "model": "m", "reasoning": "off", "api_key": "sk-secret"},
    )
    payload = build_eval_payload(session, None)
    assert "sk-secret" not in json.dumps(payload, ensure_ascii=False)
    assert "/home/teacher" not in json.dumps(payload, ensure_ascii=False)
    assert "teacher_name" not in json.dumps(payload, ensure_ascii=False)
    assert "student_name" not in json.dumps(payload, ensure_ascii=False)


def test_export_missing_token_usage_is_null_not_fabricated():
    session = ReviewSession(
        id="s1",
        teacher_id="t",
        student_id="s",
        major="",
        draft_id="new",
        paper_path="",
        created_at="",
        updated_at="",
    )
    payload = build_eval_payload(session, None)
    assert payload["token_usage"] is None
    session.token_usage = {"input": 1200, "output": 340, "cache_read": 0, "cache_write": 0}
    payload = build_eval_payload(session, None)
    assert payload["token_usage"]["input"] == 1200


def test_export_writes_json_and_two_csvs(tmp_path: Path):
    session = ReviewSession(
        id="s1",
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="new",
        paper_path="论文.docx",
        created_at="",
        updated_at="",
        findings=[
            _finding(id="a", kind="format", decision="accepted"),
            _finding(id="b", kind="content", subtype="argument", decision="rejected", rejection_reason="duplicate"),
        ],
        missed_issues=[
            MissedIssue(id="m1", category="content", problem="AI 漏掉的数据矛盾", section="2 方法"),
        ],
    )
    files = write_eval_export(session, TRACE, tmp_path)
    json_path = Path(files["json"])
    assert json_path.name == EVAL_JSON_NAME
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(payload["findings"]) == 2
    assert len(payload["teacher_missed_issues"]) == 1
    findings_csv = list(csv.reader(Path(files["findings_csv"]).open(encoding="utf-8-sig")))
    assert findings_csv[0][0] == "finding_id"
    records = {row[1]: row for row in findings_csv[1:]}
    assert set(records) == {"ai_candidate", "teacher_missed_issue"}
    assert len(records["ai_candidate"]) == 17
    summary_csv = {row[0]: row[1] for row in list(csv.reader(Path(files["summary_csv"]).open(encoding="utf-8-sig")))[1:]}
    assert summary_csv["candidate_count"] == "2"
    assert summary_csv["missed_issue_count"] == "1"
    assert summary_csv["rejection_rate"] == "0.5"
    assert summary_csv["coverage_read"] == "1"
