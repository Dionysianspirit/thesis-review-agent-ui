from __future__ import annotations

import json
from pathlib import Path

from thesis_review.comments import comment_body
from thesis_review.fixtures import write_demo_drafts
from thesis_review.service import ThesisReviewService, session_stats
from thesis_review.types import DECISION_ACCEPTED, DECISION_EDITED, DECISION_PENDING, DECISION_REJECTED
from thesis_review.word.adapter import WordAdapter

GATE_FILENAME = "teacher-gate.json"
TEACHER_EDIT_TEXT = "老师最终审稿意见：请补上可核验的实验依据，不要只写主观评价。"


def _comment_texts(adapter: WordAdapter, path: Path | str) -> list[str]:
    target = Path(path)
    if not target.is_file():
        return []
    return [item.text for item in adapter.extract_comments(adapter.open_path(target))]


def _revision_texts(adapter: WordAdapter, path: Path | str) -> list[str]:
    target = Path(path)
    if not target.is_file():
        return []
    return [
        f"{item.kind}:{item.text}"
        for item in adapter.extract_revisions(adapter.open_path(target))
        if item.text
    ]


def apply_demo_teacher_decisions(service: ThesisReviewService, session_id: str) -> dict:
    """Accept format in batch, edit one candidate, reject remaining pending items.

    Prefer editing a language finding that applies a tracked revision so the
    formal Word file contains both 批注 and 修订, matching how teachers work.
    """
    service.accept_format_findings(session_id)
    session = service.sessions.get(session_id)
    pending = [item for item in session.findings if item.teacher_decision == DECISION_PENDING]
    accepted = [item for item in session.findings if item.teacher_decision == DECISION_ACCEPTED]
    edited_id = ""
    rejected_ids: list[str] = []
    both = [
        item
        for item in pending + accepted
        if item.apply in {"revision", "both"} and item.suggested_old and item.suggested_new
    ]
    edit_target = both[0] if both else (pending[0] if pending else (accepted[0] if accepted else None))
    if edit_target is not None:
        service.decide_finding(
            session_id=session_id,
            finding_id=edit_target.id,
            decision=DECISION_EDITED,
            edited_text=TEACHER_EDIT_TEXT,
        )
        edited_id = edit_target.id
        pending = [item for item in pending if item.id != edit_target.id]
        accepted = [item for item in accepted if item.id != edit_target.id]
    for item in pending:
        service.decide_finding(session_id=session_id, finding_id=item.id, decision=DECISION_REJECTED)
        rejected_ids.append(item.id)
    session = service.sessions.get(session_id)
    if not rejected_ids:
        extras = [
            item
            for item in session.findings
            if item.id != edited_id and item.teacher_decision == DECISION_ACCEPTED
        ]
        if extras:
            service.decide_finding(
                session_id=session_id, finding_id=extras[-1].id, decision=DECISION_REJECTED
            )
            rejected_ids.append(extras[-1].id)
    session = service.sessions.get(session_id)
    rejected_problems = [
        item.original_problem or item.problem
        for item in session.findings
        if item.id in set(rejected_ids)
    ]
    return {
        "edited_id": edited_id,
        "edited_text": TEACHER_EDIT_TEXT if edited_id else "",
        "rejected_ids": rejected_ids,
        "rejected_problems": rejected_problems,
    }


def run_teacher_demo(service: ThesisReviewService, output_dir: Path, *, faux: bool = False) -> dict:
    """First-pass → teacher gate → Word comments. Used by CLI and Windows EXE smoke.

    ``faux=True`` drives the bundled Pi agent over the frozen worker TCP port so the
    packager cannot pass by silently falling back to offline rules.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    drafts = write_demo_drafts(output_dir / "demo-drafts")
    candidates = service.ingest_history(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="v1",
        data=drafts["v1"].read_bytes(),
        source_path=str(drafts["v1"]),
    )
    service.ingest_history(
        teacher_id="teacher-a",
        student_id="zhou",
        major="人工智能",
        draft_id="v2",
        data=drafts["v2"].read_bytes(),
        source_path=str(drafts["v2"]),
    )
    for item in candidates:
        service.confirm_issue(teacher_id="teacher-a", student_id="zhou", issue_id=item.id)
    result = service.review(
        teacher_id="teacher-a",
        student_id="zhou",
        draft_id="new",
        data=drafts["new"].read_bytes(),
        output_dir=output_dir,
        use_model=False,
        use_pi=faux,
        faux=faux,
        offline_fallback=not faux,
        paper_path=str(drafts["new"]),
    )
    adapter = service.adapter
    comments_before = _comment_texts(adapter, result.reviewed_path)
    revisions_before = _revision_texts(adapter, result.reviewed_path)
    source_comments = _comment_texts(adapter, result.source_path)
    source_revisions = _revision_texts(adapter, result.source_path)
    original_comments = _comment_texts(adapter, drafts["new"])
    decisions = apply_demo_teacher_decisions(service, result.session_id)
    exported = service.export_final(session_id=result.session_id, allow_pending=False)
    reviewed_path = Path(exported.get("reviewed_path") or result.reviewed_path)
    comments_after = _comment_texts(adapter, reviewed_path)
    revisions_after = _revision_texts(adapter, reviewed_path)
    blob = "\n".join(comments_after)
    session = service.sessions.get(result.session_id)
    rejected_absent = all(problem not in blob for problem in decisions["rejected_problems"] if problem)
    edited_present = (not decisions["edited_text"]) or decisions["edited_text"] in blob
    stats = session_stats(session.findings)
    tracked = [
        item
        for item in session.findings
        if item.teacher_decision in {DECISION_ACCEPTED, DECISION_EDITED}
        and item.apply in {"revision", "both"}
        and item.suggested_old
        and item.suggested_new
    ]
    rev_blob = "\n".join(revisions_after)
    tracked_present = (not tracked) or any(
        (item.suggested_old in rev_blob or item.suggested_new in rev_blob) for item in tracked
    )
    trace_path = output_dir / "new-trace.json"
    trace_ops: list[str] = []
    if trace_path.is_file():
        try:
            trace_ops = [
                str(item.get("op") or "")
                for item in (json.loads(trace_path.read_text(encoding="utf-8")).get("ops") or [])
            ]
        except json.JSONDecodeError:
            trace_ops = []
    pi_trace_ok = (not faux) or ("commit_review" in trace_ops and "run_checks" in trace_ops)
    not_written = [
        {"id": item.get("id"), "missing": item.get("missing"), "reason": item.get("reason")}
        for item in exported.get("not_written") or []
    ]
    ok = bool(
        exported.get("ok")
        and not comments_before
        and not source_comments
        and not original_comments
        and result.findings
        and comments_after
        and int(exported.get("n_exported") or 0) >= 1
        and edited_present
        and rejected_absent
        and stats["pending"] == 0
        and stats["formal"] >= 1
        and not result.warning
        and pi_trace_ok
        and not revisions_before
        and not source_revisions
        and tracked_present
        and (not tracked or revisions_after)
        and not not_written
    )
    error = ""
    if comments_before or source_comments or revisions_before or source_revisions:
        error = "AI 初审后不应把候选意见写入 Word。"
    elif not result.findings:
        error = "演示稿没有产生候选意见。"
    elif result.warning:
        error = result.warning
    elif faux and not pi_trace_ok:
        error = "Faux Agent 没有经 worker TCP 完成初审（缺少工具轨迹）。"
    elif not exported.get("ok"):
        error = str(exported.get("message") or "未能生成正式审稿稿件。")
    elif not_written:
        error = f"老师认可的意见有 {len(not_written)} 条未能完整写入正式 Word。"
    elif not comments_after:
        error = "正式稿没有写入老师认可批注。"
    elif tracked and not revisions_after:
        error = "正式稿没有写入老师认可的修订。"
    elif not tracked_present:
        error = "老师确认的文字修订没有出现在正式 Word 中。"
    elif not edited_present:
        error = "老师改写文本没有出现在正式 Word 批注中。"
    elif not rejected_absent:
        error = "已驳回意见仍出现在正式 Word 中。"
    payload = {
        "ok": ok,
        "error": error,
        "session_id": result.session_id,
        "candidates": len(result.findings),
        "comments_before": len(comments_before),
        "comments_after": len(comments_after),
        "n_exported": int(exported.get("n_exported") or 0),
        "n_approved": int(exported.get("n_approved") or 0),
        "not_written": not_written,
        "reviewed_path": str(reviewed_path),
        "source_path": result.source_path,
        "findings_path": str(result.findings_path),
        "source_comments": len(source_comments),
        "original_comments": len(original_comments),
        "revisions_before": len(revisions_before),
        "revisions_after": len(revisions_after),
        "source_revisions": len(source_revisions),
        "tracked_revisions": [
            {"old": item.suggested_old, "new": item.suggested_new} for item in tracked
        ],
        "tracked_present": tracked_present,
        "teacher_edited_text": decisions["edited_text"],
        "teacher_edited_present": edited_present,
        "rejected_problems": decisions["rejected_problems"],
        "rejected_absent": rejected_absent,
        "comment_bodies": comments_after,
        "stats": stats,
        "agent": "faux-pi" if faux else "offline",
        "pi_trace_ok": pi_trace_ok,
        "trace_ops": trace_ops,
        "kinds": sorted({item.kind for item in session.findings if item.kind}),
        "exportable_bodies": [
            comment_body(item) for item in session.findings if item.teacher_decision in {DECISION_ACCEPTED, DECISION_EDITED}
        ],
    }
    gate_path = output_dir / GATE_FILENAME
    gate_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    payload["gate_path"] = str(gate_path)
    return payload
