"""V0.8 machine-readable eval export: review-eval.json plus two CSV views.

Privacy boundaries (local-first, no upload, no telemetry):

- No API key or secret ever leaves the session store (whitelist below).
- No full paper text: only the finding's own problem/rationale and its
  minimal in-paper quote, plus location references.
- Teacher/student real names are omitted; the system-level ids stay so
  sessions remain comparable across runs and reasoning levels.

token usage is exported only when the real API reported it — otherwise
``null``. Unknown beats fabricated.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from thesis_review.evalmetrics import KIND_LABELS, compute_eval_metrics
from thesis_review.session_store import ReviewSession, now_iso
from thesis_review.types import Finding, MissedIssue, derive_kind

EVAL_SCHEMA_VERSION = "0.8"
EVAL_JSON_NAME = "review-eval.json"
EVAL_FINDINGS_CSV_NAME = "review-eval-findings.csv"
EVAL_SUMMARY_CSV_NAME = "review-eval-summary.csv"

FINDINGS_CSV_COLUMNS = (
    "finding_id",
    "record_type",
    "kind",
    "subtype",
    "category",
    "source",
    "section",
    "chapter",
    "paragraph_index",
    "anchor",
    "decision",
    "rejection_reason",
    "rejection_note",
    "teacher_edited",
    "problem",
    "rationale",
    "quote",
)


def build_eval_payload(session: ReviewSession, trace: dict | list | None = None) -> dict:
    metrics = compute_eval_metrics(session.findings, session.missed_issues, trace, session)
    return {
        "schema_version": EVAL_SCHEMA_VERSION,
        "session_id": session.id,
        "generated_at": now_iso(),
        "document": {
            "draft_id": session.draft_id,
            "file_name": Path(session.paper_path or "").name,
        },
        "participants": {
            "teacher_id": session.teacher_id,
            "student_id": session.student_id,
        },
        "model_snapshot": {
            "provider": session.model_snapshot.get("provider", ""),
            "model": session.model_snapshot.get("model", ""),
            "reasoning": session.model_snapshot.get("reasoning", ""),
            "api_key_set": bool(session.model_snapshot.get("api_key_set")),
        },
        "timing": metrics["timing"],
        "token_usage": metrics["token_usage"],
        "chapter_coverage": metrics["coverage_summary"],
        "chapter_observations": metrics["chapter_observations"],
        "metrics": {
            key: metrics[key]
            for key in (
                "candidate_count",
                "accepted_count",
                "edited_accepted_count",
                "rejected_count",
                "pending_count",
                "processed_count",
                "adopted_count",
                "acceptance_rate",
                "direct_acceptance_rate",
                "edited_acceptance_rate",
                "rejection_rate",
                "missed_issue_count",
            )
        },
        "category_metrics": metrics["category_metrics"],
        "rejection_reasons": metrics["rejection_reasons"],
        "findings": [_finding_payload(item) for item in session.findings],
        "teacher_missed_issues": [_missed_payload(item) for item in session.missed_issues],
    }


def write_eval_export(
    session: ReviewSession,
    trace: dict | list | None,
    dest_dir: Path | None = None,
) -> dict[str, str]:
    """Write the JSON bundle and the two CSV views; returns written paths."""
    payload = build_eval_payload(session, trace)
    out_dir = Path(dest_dir) if dest_dir else Path(session.output_dir or Path.cwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / EVAL_JSON_NAME
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    findings_path = out_dir / EVAL_FINDINGS_CSV_NAME
    _write_findings_csv(findings_path, session)
    summary_path = out_dir / EVAL_SUMMARY_CSV_NAME
    _write_summary_csv(summary_path, payload)
    return {
        "json": str(json_path),
        "findings_csv": str(findings_path),
        "summary_csv": str(summary_path),
    }


def _finding_payload(finding: Finding) -> dict:
    kind = derive_kind(finding)
    return {
        "id": finding.id,
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "subtype": finding.subtype,
        "category": finding.category,
        "source": finding.source,
        "section": finding.section,
        "paragraph_index": finding.paragraph_index,
        "anchor": finding.anchor,
        "problem": finding.problem,
        "rationale": finding.rationale,
        "quote": finding.quote,
        "teacher_decision": finding.teacher_decision,
        "rejection_reason": finding.rejection_reason,
        "rejection_note": finding.rejection_note,
        "teacher_edited": bool(finding.teacher_final_text or finding.teacher_final_new),
        "history_ref": bool(finding.history_refs),
        "external_sources": [
            {"title": item.title, "url": item.url} for item in finding.external_sources
        ],
    }


def _missed_payload(issue: MissedIssue) -> dict:
    return {
        "id": issue.id,
        "category": issue.category,
        "problem": issue.problem,
        "section": issue.section,
        "note": issue.note,
        "created_at": issue.created_at,
        "source": issue.source,
    }


def _write_findings_csv(path: Path, session: ReviewSession) -> None:
    """One row per AI candidate plus one per teacher missed issue.

    Chapter attribution stays in the JSON (it needs the trace's coverage
    map); the CSV keeps the raw location fields so spreadsheets can pivot
    without it.
    """
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FINDINGS_CSV_COLUMNS)
        for item in session.findings:
            payload = _finding_payload(item)
            writer.writerow(
                [
                    payload["id"],
                    "ai_candidate",
                    payload["kind"],
                    payload["subtype"],
                    payload["category"],
                    payload["source"],
                    payload["section"],
                    "",
                    payload["paragraph_index"],
                    payload["anchor"],
                    payload["teacher_decision"],
                    payload["rejection_reason"],
                    payload["rejection_note"],
                    "1" if payload["teacher_edited"] else "0",
                    payload["problem"],
                    payload["rationale"],
                    payload["quote"],
                ]
            )
        for issue in session.missed_issues:
            payload = _missed_payload(issue)
            writer.writerow(
                [
                    payload["id"],
                    payload["source"],
                    payload["category"],
                    "",
                    "",
                    payload["source"],
                    payload["section"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "0",
                    payload["problem"],
                    payload["note"],
                    "",
                ]
            )


def _write_summary_csv(path: Path, payload: dict) -> None:
    metrics = payload["metrics"]
    coverage = payload["chapter_coverage"]
    timing = payload["timing"]
    model = payload["model_snapshot"]
    usage = payload["token_usage"] or {}
    rows: list[tuple[str, object]] = [
        ("schema_version", payload["schema_version"]),
        ("session_id", payload["session_id"]),
        ("draft_id", payload["document"]["draft_id"]),
        ("provider", model["provider"]),
        ("model", model["model"]),
        ("reasoning", model["reasoning"]),
        ("review_started_at", timing.get("review_started_at", "")),
        ("review_completed_at", timing.get("review_completed_at", "")),
        ("duration_s", timing.get("duration_s", "") if timing.get("duration_s") is not None else ""),
        ("token_input", usage.get("input", "") if usage else ""),
        ("token_output", usage.get("output", "") if usage else ""),
        ("token_cache_read", usage.get("cache_read", "") if usage else ""),
        ("token_cache_write", usage.get("cache_write", "") if usage else ""),
    ]
    for key, value in metrics.items():
        rows.append((key, "" if value is None else value))
    for key in ("read", "probed", "unread", "total"):
        if key in coverage:
            rows.append((f"coverage_{key}", coverage[key]))
    for reason, count in (payload.get("rejection_reasons") or {}).items():
        rows.append((f"rejection_reason_{reason}", count))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("key", "value"))
        for key, value in rows:
            writer.writerow([key, value])
