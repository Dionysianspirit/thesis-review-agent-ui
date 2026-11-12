"""Cross-session eval aggregation: compare review sessions side by side.

One exported ``review-eval.json`` answers "how did this review go". This
module answers the next question the README poses once 5-10 reviews exist:
does adoption differ by model / reasoning level, which kinds of candidates
get rejected, and do the issues the teacher had to add by hand sit in
chapters the first pass never opened.

Rules inherited from ``evalmetrics`` (the single source of truth, reused
per session rather than re-implemented):

- ``pending`` findings are in no rate denominator.
- A rate with a zero denominator is ``None`` (unknown), never 0%.
- Pooled rates are computed from pooled counts, and every rate travels with
  its numerator and denominator, so a 100% from one decision cannot pass for
  a 100% from fifty.
- Adoption is the teacher's judgement, not model accuracy: nothing here is
  called precision or recall.

Honesty about sample size: below ``MIN_SESSIONS_FOR_COMPARISON`` sessions a
group is marked ``sample_sufficient: False``. The numbers are still shown,
because hiding them helps nobody, but they are labelled as not comparable.

Privacy: local files only. Rows carry opaque session ids, the draft id, the
model snapshot and counts. No names, no API key, no paper text, no finding
text.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from thesis_review.evalmetrics import _rate, compute_eval_metrics
from thesis_review.session_store import (
    SESSION_AWAITING,
    SESSION_COMPLETED,
    ReviewSession,
    SessionStore,
    now_iso,
)

AGGREGATE_SCHEMA_VERSION = "0.9"
AGGREGATE_JSON_NAME = "review-eval-aggregate.json"
AGGREGATE_SESSIONS_CSV_NAME = "review-eval-aggregate-sessions.csv"
AGGREGATE_GROUPS_CSV_NAME = "review-eval-aggregate-groups.csv"
# README: compare across sessions only after 5-10 reviews have accumulated.
MIN_SESSIONS_FOR_COMPARISON = 5
SESSION_SCAN_LIMIT = 5000
# A first pass that finished: candidates exist (or legitimately do not) and
# the teacher can decide. Sessions still preparing / reviewing, and failed
# ones, describe an interrupted run rather than a review.
FINISHED_STATUSES = frozenset({SESSION_AWAITING, SESSION_COMPLETED})
COVERAGE_STATUSES = ("read", "probed", "unread")
COUNT_KEYS = (
    "candidate_count",
    "accepted_count",
    "edited_accepted_count",
    "rejected_count",
    "pending_count",
    "processed_count",
    "adopted_count",
    "missed_issue_count",
)
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
INSUFFICIENT_NOTE = (
    f"样本不足（少于 {MIN_SESSIONS_FOR_COMPARISON} 次审稿），以下数字仅供参考，不能据此比较模型或思考深度。"
)

SESSION_CSV_COLUMNS = (
    "session_id",
    "draft_id",
    "created_at",
    "status",
    "provider",
    "model",
    "reasoning",
    *COUNT_KEYS,
    "acceptance_rate",
    "rejection_rate",
    "coverage_read",
    "coverage_probed",
    "coverage_unread",
    "coverage_total",
    "duration_s",
    "token_input",
    "token_output",
)
GROUP_CSV_COLUMNS = (
    "group_type",
    "key",
    "sessions",
    "sample_sufficient",
    *COUNT_KEYS,
    "acceptance_rate",
    "direct_acceptance_rate",
    "edited_acceptance_rate",
    "rejection_rate",
    "chapters",
    "duration_mean_s",
    "duration_sessions",
    "token_input",
    "token_output",
    "token_sessions",
)


def load_trace(session: ReviewSession) -> dict:
    """The run trace beside the session's results, or {} when it is gone."""
    if not session.output_dir:
        return {}
    path = Path(session.output_dir) / f"{session.draft_id}-trace.json"
    try:
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def session_row(session: ReviewSession, trace: dict | list | None = None) -> dict:
    """One session reduced to counts. Every number comes from evalmetrics."""
    metrics = compute_eval_metrics(session.findings, session.missed_issues, trace, session)
    snapshot = metrics.get("model_snapshot") or {}
    coverage = metrics.get("coverage_summary") or {}
    usage = metrics.get("token_usage") or {}
    row: dict = {
        "session_id": session.id,
        "draft_id": session.draft_id,
        "created_at": session.created_at,
        "status": session.status,
        "provider": str(snapshot.get("provider") or ""),
        "model": str(snapshot.get("model") or ""),
        "reasoning": str(snapshot.get("reasoning") or "off"),
        "used_model": bool(snapshot.get("api_key_set")),
    }
    for key in COUNT_KEYS:
        row[key] = int(metrics.get(key) or 0)
    row["acceptance_rate"] = metrics.get("acceptance_rate")
    row["rejection_rate"] = metrics.get("rejection_rate")
    row["has_coverage"] = bool(coverage)
    for status in (*COVERAGE_STATUSES, "total"):
        row[f"coverage_{status}"] = coverage.get(status) if coverage else None
    row["duration_s"] = (metrics.get("timing") or {}).get("duration_s")
    row["token_usage"] = {key: usage[key] for key in TOKEN_KEYS if key in usage} or None
    row["category_metrics"] = metrics.get("category_metrics") or []
    row["rejection_reasons"] = metrics.get("rejection_reasons") or {}
    row["chapter_observations"] = metrics.get("chapter_observations") or []
    return row


def collect_rows(store: SessionStore, *, teacher_id: str = "") -> tuple[list[dict], dict]:
    """Rows for every finished review, plus what was left out and why."""
    sessions = store.list_recent(teacher_id=teacher_id, limit=SESSION_SCAN_LIMIT)
    rows: list[dict] = []
    excluded: dict[str, int] = {}
    for session in sessions:
        if session.status not in FINISHED_STATUSES:
            excluded[session.status] = excluded.get(session.status, 0) + 1
            continue
        rows.append(session_row(session, load_trace(session)))
    rows.sort(key=lambda row: (row["created_at"], row["session_id"]))
    scope = {
        "teacher_id": teacher_id,
        "sessions_seen": len(sessions),
        "sessions_included": len(rows),
        "sessions_excluded": excluded,
    }
    return rows, scope


def build_aggregate(rows: list[dict], scope: dict | None = None) -> dict:
    overall = _pool(rows)
    sufficient = len(rows) >= MIN_SESSIONS_FOR_COMPARISON
    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "scope": dict(scope or {"sessions_included": len(rows)}),
        "sample": {
            "sessions": len(rows),
            "processed_count": overall["processed_count"],
            "min_sessions": MIN_SESSIONS_FOR_COMPARISON,
            "sufficient": sufficient,
            "note": "" if sufficient else INSUFFICIENT_NOTE,
        },
        "overall": overall,
        "by_model": _by_model(rows),
        "by_category": _by_category(rows),
        "rejection_reasons": _sum_dicts(row["rejection_reasons"] for row in rows),
        "coverage": _by_coverage(rows),
        "sessions": [_public_row(row) for row in rows],
    }


def aggregate_from_store(store: SessionStore, *, teacher_id: str = "") -> dict:
    rows, scope = collect_rows(store, teacher_id=teacher_id)
    return build_aggregate(rows, scope)


def write_aggregate(payload: dict, dest_dir: Path) -> dict[str, str]:
    """Write the JSON bundle and two CSV views; returns the written paths."""
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / AGGREGATE_JSON_NAME
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    sessions_path = out_dir / AGGREGATE_SESSIONS_CSV_NAME
    _write_csv(sessions_path, SESSION_CSV_COLUMNS, payload.get("sessions") or [])
    groups_path = out_dir / AGGREGATE_GROUPS_CSV_NAME
    _write_csv(groups_path, GROUP_CSV_COLUMNS, _group_rows(payload))
    return {
        "json": str(json_path),
        "sessions_csv": str(sessions_path),
        "groups_csv": str(groups_path),
    }


def summary_lines(payload: dict) -> list[str]:
    """Count-level summary for the CLI; no finding text, no names."""
    sample = payload["sample"]
    overall = payload["overall"]
    lines = [
        f"sessions: {sample['sessions']}  processed: {overall['processed_count']}  "
        f"adopted: {overall['adopted_count']}  rejected: {overall['rejected_count']}  "
        f"pending: {overall['pending_count']}  missed: {overall['missed_issue_count']}",
        f"acceptance_rate: {_show_rate(overall['acceptance_rate'])} "
        f"({overall['adopted_count']}/{overall['processed_count']})",
    ]
    for group in payload["by_model"]:
        lines.append(
            f"  {group['key']}: sessions={group['sessions']} "
            f"acceptance={_show_rate(group['acceptance_rate'])} "
            f"({group['adopted_count']}/{group['processed_count']})"
            + ("" if group["sample_sufficient"] else "  [样本不足]")
        )
    if sample["note"]:
        lines.append(sample["note"])
    return lines


# ---------------------------------------------------------------------------
# pooling
# ---------------------------------------------------------------------------


def _pool(rows: list[dict]) -> dict:
    """Pooled counts and the rates derived from them (never a mean of rates)."""
    pooled = {key: sum(int(row.get(key) or 0) for row in rows) for key in COUNT_KEYS}
    processed = pooled["processed_count"]
    pooled["sessions"] = len(rows)
    pooled["sample_sufficient"] = len(rows) >= MIN_SESSIONS_FOR_COMPARISON
    pooled["acceptance_rate"] = _rate(pooled["adopted_count"], processed)
    pooled["direct_acceptance_rate"] = _rate(pooled["accepted_count"], processed)
    pooled["edited_acceptance_rate"] = _rate(pooled["edited_accepted_count"], processed)
    pooled["rejection_rate"] = _rate(pooled["rejected_count"], processed)
    return pooled


def _by_model(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault(_model_key(row), []).append(row)
    out = []
    for key in sorted(groups):
        members = groups[key]
        group = _pool(members)
        group.update(
            {
                "key": " / ".join(key),
                "provider": key[0],
                "model": key[1],
                "reasoning": key[2],
                "duration": _duration(members),
                "token_usage": _tokens(members),
            }
        )
        out.append(group)
    return out


def _model_key(row: dict) -> tuple[str, str, str]:
    if not row.get("used_model"):
        # No key was configured: the run was rules + string recall, whatever
        # model name happens to sit in the settings.
        return ("offline", "规则与历史召回", "off")
    return (row["provider"] or "unknown", row["model"] or "unknown", row["reasoning"] or "off")


def _by_category(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        for item in row["category_metrics"]:
            key = (str(item.get("category") or ""), str(item.get("subtype") or ""))
            group = groups.setdefault(
                key,
                {
                    "category": key[0],
                    "subtype": key[1],
                    "key": key[0] + (f" · {key[1]}" if key[1] else ""),
                    "sessions": 0,
                    "candidate_count": 0,
                    "accepted_count": 0,
                    "edited_accepted_count": 0,
                    "rejected_count": 0,
                    "pending_count": 0,
                },
            )
            group["sessions"] += 1
            group["candidate_count"] += int(item.get("candidate_count") or 0)
            group["accepted_count"] += int(item.get("accepted") or 0)
            group["edited_accepted_count"] += int(item.get("edited_accepted") or 0)
            group["rejected_count"] += int(item.get("rejected") or 0)
            group["pending_count"] += int(item.get("pending") or 0)
    out = []
    for key in sorted(groups):
        group = groups[key]
        group["adopted_count"] = group["accepted_count"] + group["edited_accepted_count"]
        group["processed_count"] = group["adopted_count"] + group["rejected_count"]
        group["acceptance_rate"] = _rate(group["adopted_count"], group["processed_count"])
        group["rejection_rate"] = _rate(group["rejected_count"], group["processed_count"])
        group["sample_sufficient"] = group["sessions"] >= MIN_SESSIONS_FOR_COMPARISON
        out.append(group)
    return out


def _by_coverage(rows: list[dict]) -> dict:
    """Where candidates and teacher-added issues landed, by chapter contact.

    Only sessions with a coverage map can say anything here (offline rule
    runs have no agent navigation), and only items that could be tied to a
    chapter are counted per status. Everything else is reported as
    unlocated instead of being guessed into a bucket.
    """
    statuses = {
        status: {
            "coverage_status": status,
            "key": status,
            "chapters": 0,
            "candidate_count": 0,
            "adopted_count": 0,
            "rejected_count": 0,
            "missed_issue_count": 0,
        }
        for status in COVERAGE_STATUSES
    }
    with_coverage = [row for row in rows if row["has_coverage"]]
    located_candidates = located_missed = 0
    for row in with_coverage:
        for chapter in row["chapter_observations"]:
            status = str(chapter.get("coverage_status") or "")
            bucket = statuses.get(status if status in statuses else "unread")
            bucket["chapters"] += 1
            bucket["candidate_count"] += int(chapter.get("ai_candidates") or 0)
            bucket["adopted_count"] += int(chapter.get("accepted") or 0) + int(
                chapter.get("edited_accepted") or 0
            )
            bucket["rejected_count"] += int(chapter.get("rejected") or 0)
            bucket["missed_issue_count"] += int(chapter.get("teacher_missed_issues") or 0)
            located_candidates += int(chapter.get("ai_candidates") or 0)
            located_missed += int(chapter.get("teacher_missed_issues") or 0)
    for bucket in statuses.values():
        processed = bucket["adopted_count"] + bucket["rejected_count"]
        bucket["processed_count"] = processed
        bucket["acceptance_rate"] = _rate(bucket["adopted_count"], processed)
    return {
        "sessions_with_coverage": len(with_coverage),
        "sessions_without_coverage": len(rows) - len(with_coverage),
        "by_status": [statuses[status] for status in COVERAGE_STATUSES],
        "candidates_unlocated": sum(row["candidate_count"] for row in with_coverage)
        - located_candidates,
        "missed_unlocated": sum(row["missed_issue_count"] for row in with_coverage) - located_missed,
    }


def _duration(rows: list[dict]) -> dict:
    values = [float(row["duration_s"]) for row in rows if _positive(row.get("duration_s"))]
    return {
        "sessions_with_data": len(values),
        "mean_s": round(sum(values) / len(values), 2) if values else None,
        "total_s": round(sum(values), 2) if values else None,
    }


def _tokens(rows: list[dict]) -> dict:
    """Sum of real API usage. Sessions without usage are counted, not zero-filled."""
    reported = [row["token_usage"] for row in rows if row.get("token_usage")]
    totals: dict = {"sessions_with_data": len(reported)}
    for key in TOKEN_KEYS:
        values = [usage[key] for usage in reported if isinstance(usage.get(key), (int, float))]
        totals[key] = sum(values) if values else None
    return totals


def _positive(value: object) -> bool:
    try:
        return float(value) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _sum_dicts(dicts) -> dict[str, int]:
    total: dict[str, int] = {}
    for item in dicts:
        for key, value in (item or {}).items():
            total[str(key)] = total.get(str(key), 0) + int(value or 0)
    return dict(sorted(total.items()))


def _public_row(row: dict) -> dict:
    """The per-session row as exported: flat counts, nothing nested or textual."""
    usage = row.get("token_usage") or {}
    public = {key: row.get(key) for key in SESSION_CSV_COLUMNS if key in row}
    public["token_input"] = usage.get("input")
    public["token_output"] = usage.get("output")
    if not row.get("used_model"):
        public["provider"], public["model"], public["reasoning"] = _model_key(row)
    return public


def _group_rows(payload: dict) -> list[dict]:
    rows: list[dict] = [dict(payload["overall"], group_type="overall", key="all")]
    for group in payload["by_model"]:
        row = dict(group, group_type="model")
        row["duration_mean_s"] = group["duration"]["mean_s"]
        row["duration_sessions"] = group["duration"]["sessions_with_data"]
        row["token_input"] = group["token_usage"]["input"]
        row["token_output"] = group["token_usage"]["output"]
        row["token_sessions"] = group["token_usage"]["sessions_with_data"]
        rows.append(row)
    rows.extend(dict(group, group_type="category") for group in payload["by_category"])
    coverage = payload["coverage"]
    for bucket in coverage["by_status"]:
        rows.append(
            dict(bucket, group_type="coverage_status", sessions=coverage["sessions_with_coverage"])
        )
    for reason, count in payload["rejection_reasons"].items():
        rows.append({"group_type": "rejection_reason", "key": reason, "rejected_count": count})
    return rows


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(column)) for column in columns])


def _cell(value: object) -> object:
    if value is None:
        return ""  # unknown stays blank; a 0 would claim a measurement
    if isinstance(value, bool):
        return "1" if value else "0"
    return value


def _show_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"
