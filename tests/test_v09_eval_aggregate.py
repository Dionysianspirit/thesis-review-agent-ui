"""V0.9 cross-session eval aggregation: pooled counts, honest rates, privacy."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from thesis_review.cli import build_service, main as cli_main
from thesis_review.evalaggregate import (
    AGGREGATE_GROUPS_CSV_NAME,
    AGGREGATE_JSON_NAME,
    AGGREGATE_SESSIONS_CSV_NAME,
    MIN_SESSIONS_FOR_COMPARISON,
    aggregate_from_store,
    build_aggregate,
    session_row,
    summary_lines,
    write_aggregate,
)
from thesis_review.evalexport import build_eval_payload, write_eval_export
from thesis_review.evalmetrics import owned_trace, review_duration_s
from thesis_review.session_store import ReviewSession, SessionStore, now_iso
from thesis_review.types import Finding, MissedIssue

SECRET = "sk-should-never-appear-0123456789"
TEACHER_NAME = "王老师真名"
STUDENT_NAME = "李同学真名"
PAPER_SENTENCE = "这是论文原文里的一句完整的话不应该出现在汇总里。"


def finding(fid: str, decision: str, *, kind: str = "content", subtype: str = "argument",
            paragraph: int = 3, reason: str = "") -> Finding:
    return Finding(
        id=fid,
        category="B",
        source="argument",
        problem=f"问题描述 {fid}",
        rationale=f"判断依据 {fid}",
        quote=PAPER_SENTENCE,
        anchor=f"P{paragraph}#abcd",
        paragraph_index=paragraph,
        apply="comment",
        kind=kind,
        subtype=subtype,
        teacher_decision=decision,
        rejection_reason=reason,
    )


def trace_for(session_id: str) -> dict:
    """Chapters: 摘要 P1-2 (read), 2 方法 P3-5 (probed), 3 实验 P6-9 (unread)."""
    return {
        "session_id": session_id,
        "ops": [],
        "coverage": {
            "sections": [
                {"ordinal": 1, "end": 3, "title": "摘要", "n_paras": 2, "status": "read", "reads": 1},
                {"ordinal": 3, "end": 6, "title": "2 方法", "n_paras": 3, "status": "probed", "reads": 0},
                {"ordinal": 6, "end": 10, "title": "3 实验", "n_paras": 4, "status": "unread", "reads": 0},
            ]
        },
    }


def add_session(store: SessionStore, tmp: Path, name: str, findings: list[Finding], *,
                model: str = "model-a", reasoning: str = "off", key_set: bool = True,
                status: str = "completed", missed: list[MissedIssue] | None = None,
                with_trace: bool = True, duration: float | None = 120.0,
                usage: dict | None = None, teacher_id: str = "teacher-a") -> ReviewSession:
    out_dir = tmp / name
    out_dir.mkdir(parents=True, exist_ok=True)
    session = ReviewSession(
        id=f"session-{name}",
        teacher_id=teacher_id,
        teacher_name=TEACHER_NAME,
        student_id="student-1",
        student_name=STUDENT_NAME,
        major="人工智能",
        draft_id=f"draft-{name}",
        paper_path=str(tmp / f"{name}.docx"),
        created_at=now_iso(),
        updated_at=now_iso(),
        status=status,
        model_snapshot={"provider": "openai-compatible", "model": model, "reasoning": reasoning,
                        "api_key_set": key_set, "api_key": SECRET},
        findings=findings,
        output_dir=str(out_dir),
        quality={"duration_s": duration} if duration is not None else {},
        missed_issues=missed or [],
        token_usage=usage or {},
    )
    if with_trace:
        (out_dir / f"{session.draft_id}-trace.json").write_text(
            json.dumps(trace_for(session.id)), encoding="utf-8"
        )
    store.save(session)
    return session


def make_store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "review-sessions.sqlite")


def test_pooled_rates_use_pooled_counts_and_exclude_pending(tmp_path):
    store = make_store(tmp_path)
    # session 1: 1 processed, adopted -> 100%.  session 2: 4 processed, 1 adopted -> 25%.
    add_session(store, tmp_path, "one", [finding("a", "accepted"), finding("p", "pending")])
    add_session(store, tmp_path, "two", [finding("b", "edited_accepted"), finding("c", "rejected"),
                                         finding("d", "rejected"), finding("e", "rejected")])
    overall = aggregate_from_store(store)["overall"]
    assert overall["candidate_count"] == 6
    assert overall["pending_count"] == 1
    assert overall["processed_count"] == 5  # pending is in no denominator
    assert overall["adopted_count"] == 2
    assert overall["acceptance_rate"] == 0.4  # 2/5 pooled, not the 62.5% mean of 100% and 25%
    assert overall["direct_acceptance_rate"] == 0.2
    assert overall["edited_acceptance_rate"] == 0.2
    assert overall["rejection_rate"] == 0.6


def test_rates_are_none_not_zero_when_nothing_is_processed(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "undecided", [finding("a", "pending"), finding("b", "pending")])
    payload = aggregate_from_store(store)
    assert payload["overall"]["processed_count"] == 0
    for key in ("acceptance_rate", "direct_acceptance_rate", "edited_acceptance_rate", "rejection_rate"):
        assert payload["overall"][key] is None
    assert payload["by_model"][0]["acceptance_rate"] is None
    assert payload["sessions"][0]["acceptance_rate"] is None
    assert "—" in "\n".join(summary_lines(payload))


def test_empty_store_is_a_valid_empty_aggregate(tmp_path):
    payload = aggregate_from_store(make_store(tmp_path))
    assert payload["sample"]["sessions"] == 0
    assert payload["sample"]["sufficient"] is False
    assert payload["overall"]["acceptance_rate"] is None
    assert payload["by_model"] == [] and payload["by_category"] == [] and payload["sessions"] == []
    written = write_aggregate(payload, tmp_path / "out")
    assert Path(written["json"]).is_file()


def test_only_finished_reviews_are_counted_and_exclusions_are_reported(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "done", [finding("a", "accepted")])
    add_session(store, tmp_path, "waiting", [finding("b", "rejected")], status="awaiting_teacher")
    add_session(store, tmp_path, "running", [finding("c", "accepted")], status="reviewing")
    add_session(store, tmp_path, "broken", [finding("d", "accepted")], status="failed")
    payload = aggregate_from_store(store)
    assert payload["scope"]["sessions_seen"] == 4
    assert payload["scope"]["sessions_included"] == 2
    assert payload["scope"]["sessions_excluded"] == {"reviewing": 1, "failed": 1}
    assert payload["overall"]["candidate_count"] == 2


def test_groups_by_model_and_reasoning_with_sample_flag(tmp_path):
    store = make_store(tmp_path)
    for index in range(MIN_SESSIONS_FOR_COMPARISON):
        add_session(store, tmp_path, f"off{index}", [finding(f"a{index}", "accepted")],
                    usage={"input": 100, "output": 10})
    add_session(store, tmp_path, "high", [finding("h1", "rejected"), finding("h2", "accepted")],
                reasoning="high", duration=None)
    add_session(store, tmp_path, "offline", [finding("o1", "rejected")], key_set=False, model="ignored-name")
    payload = aggregate_from_store(store)
    groups = {group["key"]: group for group in payload["by_model"]}
    assert set(groups) == {
        "openai-compatible / model-a / off",
        "openai-compatible / model-a / high",
        "offline / 规则与历史召回 / off",
    }
    off = groups["openai-compatible / model-a / off"]
    assert off["sessions"] == MIN_SESSIONS_FOR_COMPARISON and off["sample_sufficient"] is True
    assert off["acceptance_rate"] == 1.0
    assert off["token_usage"] == {"sessions_with_data": 5, "input": 500, "output": 50,
                                  "cache_read": None, "cache_write": None, "reasoning": None}
    assert off["duration"] == {"sessions_with_data": 5, "mean_s": 120.0, "total_s": 600.0}
    high = groups["openai-compatible / model-a / high"]
    assert high["sessions"] == 1 and high["sample_sufficient"] is False
    assert high["acceptance_rate"] == 0.5
    # no measurement and no usage: reported as absent, never as zero
    assert high["duration"] == {"sessions_with_data": 0, "mean_s": None, "total_s": None}
    assert high["token_usage"]["sessions_with_data"] == 0 and high["token_usage"]["input"] is None
    assert payload["sample"]["sufficient"] is True and payload["sample"]["note"] == ""
    assert any("样本不足" in line for line in summary_lines(payload))


def test_small_sample_is_labelled(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "only", [finding("a", "accepted")])
    payload = aggregate_from_store(store)
    assert payload["sample"]["sufficient"] is False
    assert "样本不足" in payload["sample"]["note"]
    assert payload["overall"]["sample_sufficient"] is False


def test_categories_and_rejection_reasons_are_pooled(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "one", [
        finding("a", "accepted", subtype="argument"),
        finding("b", "rejected", subtype="data_consistency", reason="false_positive"),
        finding("f", "accepted", kind="format", subtype=""),
    ])
    add_session(store, tmp_path, "two", [
        finding("c", "rejected", subtype="data_consistency", reason="false_positive"),
        finding("d", "rejected", subtype="data_consistency"),
        finding("e", "pending", subtype="data_consistency"),
    ])
    payload = aggregate_from_store(store)
    by_key = {group["key"]: group for group in payload["by_category"]}
    data = by_key["content · data_consistency"]
    assert (data["sessions"], data["candidate_count"], data["rejected_count"], data["pending_count"]) == (2, 4, 3, 1)
    assert data["processed_count"] == 3 and data["acceptance_rate"] == 0.0
    assert by_key["format"]["acceptance_rate"] == 1.0
    assert payload["rejection_reasons"] == {"false_positive": 2, "unclassified": 1}


def test_coverage_table_shows_where_candidates_and_missed_issues_landed(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "covered", [
        finding("a", "accepted", paragraph=1),   # 摘要: read
        finding("b", "rejected", paragraph=4),   # 2 方法: probed
        finding("z", "accepted", paragraph=0),   # no paragraph: cannot be located
    ], missed=[
        MissedIssue(id="m1", problem="实验缺少对照", section="3 实验"),   # unread chapter
        MissedIssue(id="m2", problem="位置不明的问题", section="某处"),   # no chapter match
    ])
    add_session(store, tmp_path, "rules-only", [finding("c", "accepted", paragraph=1)], with_trace=False)
    coverage = aggregate_from_store(store)["coverage"]
    assert coverage["sessions_with_coverage"] == 1
    assert coverage["sessions_without_coverage"] == 1
    by_status = {bucket["coverage_status"]: bucket for bucket in coverage["by_status"]}
    assert by_status["read"]["chapters"] == 1 and by_status["read"]["adopted_count"] == 1
    assert by_status["probed"]["rejected_count"] == 1 and by_status["probed"]["acceptance_rate"] == 0.0
    assert by_status["unread"]["candidate_count"] == 0
    assert by_status["unread"]["missed_issue_count"] == 1  # the teacher found it where the AI never looked
    assert by_status["unread"]["acceptance_rate"] is None
    assert coverage["candidates_unlocated"] == 1
    assert coverage["missed_unlocated"] == 1


def test_trace_from_another_session_is_not_borrowed(tmp_path):
    """The trace file is keyed by draft id, so a later review overwrites it."""
    store = make_store(tmp_path)
    session = add_session(store, tmp_path, "old", [finding("a", "accepted", paragraph=1)])
    trace_path = Path(session.output_dir) / f"{session.draft_id}-trace.json"
    trace_path.write_text(json.dumps(trace_for("some-newer-session")), encoding="utf-8")
    assert owned_trace(trace_for("some-newer-session"), session) is None
    payload = aggregate_from_store(store)
    assert payload["coverage"]["sessions_with_coverage"] == 0
    assert payload["sessions"][0]["coverage_total"] is None


def test_duration_falls_back_to_timestamps_and_is_none_when_unknown(tmp_path):
    store = make_store(tmp_path)
    session = add_session(store, tmp_path, "s", [finding("a", "accepted")], duration=0.0)
    assert review_duration_s(session) is None
    session.review_started_at = "2026-09-16T08:00:00+00:00"
    session.review_completed_at = "2026-09-16T08:03:30+00:00"
    assert review_duration_s(session) == 210.0
    session.quality = {"duration_s": 42.5}
    assert review_duration_s(session) == 42.5
    assert session_row(session)["duration_s"] == 42.5


def test_exports_hold_counts_only_no_names_keys_or_paper_text(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "one", [finding("a", "accepted"), finding("b", "rejected", reason="duplicate")],
                missed=[MissedIssue(id="m1", problem="老师补录的漏检内容", section="3 实验")])
    written = write_aggregate(aggregate_from_store(store), tmp_path / "out")
    assert {Path(path).name for path in written.values()} == {
        AGGREGATE_JSON_NAME, AGGREGATE_SESSIONS_CSV_NAME, AGGREGATE_GROUPS_CSV_NAME,
    }
    blob = "".join(Path(path).read_text(encoding="utf-8-sig") for path in written.values())
    for forbidden in (SECRET, TEACHER_NAME, STUDENT_NAME, PAPER_SENTENCE, "问题描述", "判断依据",
                      "老师补录的漏检内容", "api_key"):
        assert forbidden not in blob
    payload = json.loads(Path(written["json"]).read_text(encoding="utf-8"))
    assert payload["schema_version"] == "0.9"
    assert set(payload["sessions"][0]) >= {"session_id", "draft_id", "model", "processed_count"}
    assert "teacher_name" not in payload["sessions"][0] and "student_id" not in payload["sessions"][0]


def test_csv_views_keep_unknown_blank(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "undecided", [finding("a", "pending")], duration=None, with_trace=False)
    written = write_aggregate(aggregate_from_store(store), tmp_path / "out")
    with open(written["sessions_csv"], encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["acceptance_rate"] == "" and row["duration_s"] == "" and row["coverage_total"] == ""
    assert row["token_input"] == "" and row["pending_count"] == "1"
    with open(written["groups_csv"], encoding="utf-8-sig", newline="") as handle:
        groups = list(csv.DictReader(handle))
    overall = next(item for item in groups if item["group_type"] == "overall")
    assert overall["acceptance_rate"] == "" and overall["sample_sufficient"] == "0"
    assert {item["group_type"] for item in groups} >= {"overall", "model", "category", "coverage_status"}


def test_teacher_filter(tmp_path):
    store = make_store(tmp_path)
    add_session(store, tmp_path, "mine", [finding("a", "accepted")])
    add_session(store, tmp_path, "theirs", [finding("b", "rejected")], teacher_id="teacher-b")
    assert aggregate_from_store(store)["sample"]["sessions"] == 2
    mine = aggregate_from_store(store, teacher_id="teacher-a")
    assert mine["sample"]["sessions"] == 1 and mine["overall"]["acceptance_rate"] == 1.0


def test_build_aggregate_accepts_rows_directly():
    payload = build_aggregate([])
    assert payload["scope"] == {"sessions_included": 0}


def test_service_and_cli_entry_points(tmp_path, capsys):
    home = tmp_path / "home"
    service = build_service(home)
    add_session(service.sessions, tmp_path, "one", [finding("a", "accepted"), finding("b", "rejected")])
    result = service.export_eval_aggregate(dest=tmp_path / "svc-out")
    assert result["ok"] and Path(result["files"]["json"]).is_file()
    assert result["sample"]["sessions"] == 1
    assert service.aggregate_eval()["overall"]["acceptance_rate"] == 0.5

    code = cli_main(["--home", str(home), "eval-aggregate", "--out", str(tmp_path / "cli-out")])
    assert code == 0
    printed = capsys.readouterr().out
    assert "sessions: 1" in printed and "acceptance_rate: 50.0% (1/2)" in printed
    assert "样本不足" in printed
    for forbidden in (TEACHER_NAME, STUDENT_NAME, PAPER_SENTENCE, SECRET):
        assert forbidden not in printed
    assert (tmp_path / "cli-out" / AGGREGATE_JSON_NAME).is_file()


def test_single_session_export_now_names_each_findings_chapter(tmp_path):
    store = make_store(tmp_path)
    session = add_session(store, tmp_path, "one", [
        finding("a", "accepted", paragraph=1), finding("b", "rejected", paragraph=7),
        finding("z", "pending", paragraph=0),
    ], missed=[MissedIssue(id="m1", problem="实验缺少对照", section="3 实验"),
               MissedIssue(id="m2", problem="位置不明", section="")])
    trace = trace_for(session.id)
    payload = build_eval_payload(session, trace)
    by_id = {item["id"]: item for item in payload["findings"]}
    assert (by_id["a"]["chapter"], by_id["a"]["chapter_coverage_status"]) == ("摘要", "read")
    assert (by_id["b"]["chapter"], by_id["b"]["chapter_coverage_status"]) == ("3 实验", "unread")
    assert (by_id["z"]["chapter"], by_id["z"]["chapter_coverage_status"]) == ("", "")
    missed = {item["id"]: item for item in payload["teacher_missed_issues"]}
    assert (missed["m1"]["chapter"], missed["m1"]["chapter_coverage_status"]) == ("3 实验", "unread")
    assert missed["m2"]["chapter"] == ""

    written = write_eval_export(session, trace, tmp_path / "export")
    with open(written["findings_csv"], encoding="utf-8-sig", newline="") as handle:
        rows = {row["finding_id"]: row for row in csv.DictReader(handle)}
    assert rows["a"]["chapter"] == "摘要" and rows["b"]["chapter"] == "3 实验"
    assert rows["m1"]["chapter"] == "3 实验"
    assert rows["z"]["chapter"] == ""
    assert "chapter_coverage_status" not in rows["a"]  # CSV layout unchanged; status is in the JSON

    # no trace at all: the export still works and the new fields stay blank
    bare = build_eval_payload(session, None)
    assert all(item["chapter"] == "" for item in bare["findings"])
