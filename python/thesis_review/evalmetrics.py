"""V0.8 review metrics: teacher-decision quality derived from session data.

Single source of truth: every number here is computed from the session's
findings / missed issues and the run trace at call time. Nothing is stored
twice, so an edited decision immediately changes the metrics.

Rate denominators (the one convention this module owns):

- ``processed_count = accepted + edited_accepted + rejected`` — decisions the
  teacher has actually made. ``pending`` findings are excluded from every
  rate denominator: an undecided candidate says nothing about quality.
- ``acceptance_rate = (accepted + edited_accepted) / processed_count`` — the
  teacher's final adoption rate. Both accepted flavors count as adopted.
- ``direct_acceptance_rate`` / ``edited_acceptance_rate`` split that adoption.
- ``rejection_rate = rejected / processed_count``.

When no decision has been made (``processed_count == 0``) every rate is
``None`` — unknown, never a fabricated 0%.
"""
from __future__ import annotations

from datetime import datetime

from thesis_review.types import (
    DECISION_ACCEPTED,
    DECISION_EDITED,
    DECISION_PENDING,
    DECISION_REJECTED,
    Finding,
    MissedIssue,
    derive_kind,
)

KIND_LABELS = {
    "format": "格式",
    "language": "语言",
    "content": "内容",
    "history": "历史复查",
    "external": "外部核验",
}


def compute_eval_metrics(
    findings: list,
    missed_issues: list[MissedIssue | dict] | None = None,
    trace: dict | list | None = None,
    session=None,
) -> dict:
    decisions = {DECISION_PENDING: 0, DECISION_ACCEPTED: 0, DECISION_EDITED: 0, DECISION_REJECTED: 0}
    by_category: dict[tuple[str, str], dict] = {}
    rejection_reasons: dict[str, int] = {}
    for raw in findings or []:
        item = raw if isinstance(raw, Finding) else Finding.from_dict(raw if isinstance(raw, dict) else {})
        decision = item.teacher_decision or DECISION_PENDING
        decisions[decision] = decisions.get(decision, 0) + 1
        row = _category_row(by_category, item)
        row["candidate_count"] += 1
        if decision == DECISION_ACCEPTED:
            row["accepted"] += 1
        elif decision == DECISION_EDITED:
            row["edited_accepted"] += 1
        elif decision == DECISION_REJECTED:
            row["rejected"] += 1
            if item.rejection_reason:
                rejection_reasons[item.rejection_reason] = rejection_reasons.get(item.rejection_reason, 0) + 1
            else:
                rejection_reasons["unclassified"] = rejection_reasons.get("unclassified", 0) + 1
        else:
            row["pending"] += 1
    processed = decisions[DECISION_ACCEPTED] + decisions[DECISION_EDITED] + decisions[DECISION_REJECTED]
    adopted = decisions[DECISION_ACCEPTED] + decisions[DECISION_EDITED]
    missed = [_missed_dict(item) for item in missed_issues or []]
    trace = owned_trace(trace, session)
    return {
        "candidate_count": len(findings or []),
        "accepted_count": decisions[DECISION_ACCEPTED],
        "edited_accepted_count": decisions[DECISION_EDITED],
        "rejected_count": decisions[DECISION_REJECTED],
        "pending_count": decisions[DECISION_PENDING],
        "processed_count": processed,
        "adopted_count": adopted,
        "acceptance_rate": _rate(adopted, processed),
        "direct_acceptance_rate": _rate(decisions[DECISION_ACCEPTED], processed),
        "edited_acceptance_rate": _rate(decisions[DECISION_EDITED], processed),
        "rejection_rate": _rate(decisions[DECISION_REJECTED], processed),
        "category_metrics": _sorted_categories(by_category),
        "rejection_reasons": rejection_reasons,
        "missed_issue_count": len(missed),
        "chapter_observations": chapter_observations(trace, findings or [], missed),
        "coverage_summary": coverage_summary(trace),
        "timing": {
            "review_started_at": getattr(session, "review_started_at", "") or "",
            "review_completed_at": getattr(session, "review_completed_at", "") or "",
            "duration_s": review_duration_s(session),
        },
        "model_snapshot": dict(getattr(session, "model_snapshot", None) or {}),
        "token_usage": _token_usage(session),
    }


def category_label(key: str) -> str:
    return KIND_LABELS.get(key, key)


def owned_trace(trace: dict | list | None, session=None) -> dict | list | None:
    """Drop a trace that was written by a different review session.

    The trace file is keyed by draft id (``<draft>-trace.json``), so reviewing
    the same file again overwrites it, and an offline re-run leaves the older
    run's file in place. A trace that names another session says nothing
    about this one: no coverage data beats someone else's coverage data.
    Traces without a session id (older versions, hand-built ones) pass through.
    """
    if session is None or not isinstance(trace, dict):
        return trace
    owner = str(trace.get("session_id") or "")
    current = str(getattr(session, "id", "") or "")
    if owner and current and owner != current:
        return None
    return trace


def review_duration_s(session) -> float | None:
    """Seconds the first pass took, or None when it is not known.

    ``quality.duration_s`` is the measured wall time, but the quality block is
    rebuilt when the formal Word is generated and the measurement is not
    carried over, which used to turn every later eval export into
    ``duration_s: 0``. A non-positive value is therefore not a measurement:
    fall back to the stored start/end timestamps, and report unknown rather
    than zero when those do not give a positive span either.
    """
    if session is None:
        return None
    quality = getattr(session, "quality", None) or {}
    try:
        measured = float(quality.get("duration_s") or 0)
    except (TypeError, ValueError):
        measured = 0.0
    if measured > 0:
        return round(measured, 2)
    started = _parse_iso(getattr(session, "review_started_at", ""))
    completed = _parse_iso(getattr(session, "review_completed_at", ""))
    if started is None or completed is None:
        return None
    try:
        span = (completed - started).total_seconds()
    except TypeError:  # one timestamp carries a UTC offset, the other does not
        return None
    return round(span, 2) if span > 0 else None


def finding_chapter(trace: dict | list | None, paragraph_index: int) -> tuple[str, str]:
    """(chapter title, coverage status) for a paragraph; ("", "") when unknown.

    Same paragraph-range rule as ``chapter_observations``, exposed so the
    per-finding export and the per-chapter table can never disagree.
    """
    sections = _coverage_sections(trace)
    try:
        index = int(paragraph_index or 0)
    except (TypeError, ValueError):
        return "", ""
    title = _chapter_for_paragraph(sections, index)
    if title is None:
        return "", ""
    return title, _status_of(sections, title)


def missed_issue_chapter(trace: dict | list | None, section_text: str) -> tuple[str, str]:
    """(chapter title, coverage status) for a teacher-written location.

    Free text only maps to a chapter when it clearly overlaps a chapter
    title; anything else stays unlocated.
    """
    sections = _coverage_sections(trace)
    title = _chapter_for_missed_section(sections, str(section_text or ""))
    if title is None:
        return "", ""
    return title, _status_of(sections, title)


def chapter_observations(
    trace: dict | list | None,
    findings: list,
    missed_issues: list[dict] | None = None,
) -> list[dict]:
    """Per-chapter facts: coverage status plus where findings landed.

    AI findings map to chapters by paragraph range from the trace's coverage
    map. Teacher missed issues only match when the free-text section overlaps
    a chapter title; everything else stays "（未定位）" — facts only, no forced
    causal assignment.
    """
    sections = _coverage_sections(trace)
    if not sections:
        return []
    rows: dict[str, dict] = {
        str(section["title"]): {
            "chapter": str(section["title"]),
            "coverage_status": str(section["status"]),
            "ai_candidates": 0,
            "accepted": 0,
            "edited_accepted": 0,
            "rejected": 0,
            "teacher_missed_issues": 0,
        }
        for section in sections
    }
    for raw in findings or []:
        item = raw if isinstance(raw, Finding) else Finding.from_dict(raw if isinstance(raw, dict) else {})
        title = _chapter_for_paragraph(sections, int(item.paragraph_index or 0))
        row = rows.get(title)
        if row is None:
            continue
        row["ai_candidates"] += 1
        decision = item.teacher_decision or DECISION_PENDING
        if decision == DECISION_ACCEPTED:
            row["accepted"] += 1
        elif decision == DECISION_EDITED:
            row["edited_accepted"] += 1
        elif decision == DECISION_REJECTED:
            row["rejected"] += 1
    for issue in missed_issues or []:
        title = _chapter_for_missed_section(sections, str(issue.get("section") or ""))
        if title is None:
            continue
        rows[title]["teacher_missed_issues"] += 1
    return list(rows.values())


def coverage_summary(trace: dict | list | None) -> dict:
    sections = _coverage_sections(trace)
    if not sections:
        return {}
    counts = {"read": 0, "probed": 0, "unread": 0}
    for section in sections:
        status = str(section["status"]) if section["status"] in counts else "unread"
        counts[status] += 1
    return {
        "read": counts["read"],
        "probed": counts["probed"],
        "unread": counts["unread"],
        "total": len(sections),
    }


def _category_row(by_category: dict[tuple[str, str], dict], item: Finding) -> dict:
    key = (derive_kind(item), item.subtype or "")
    if key not in by_category:
        by_category[key] = {
            "category": key[0],
            "subtype": key[1],
            "candidate_count": 0,
            "accepted": 0,
            "edited_accepted": 0,
            "rejected": 0,
            "pending": 0,
        }
    return by_category[key]


def _sorted_categories(by_category: dict[tuple[str, str], dict]) -> list[dict]:
    rows = []
    for key in sorted(by_category):
        row = by_category[key]
        # Same denominator convention as the global rates: only processed
        # decisions count; pending stays out of every acceptance rate.
        processed = row["accepted"] + row["edited_accepted"] + row["rejected"]
        row["acceptance_rate"] = _rate(row["accepted"] + row["edited_accepted"], processed)
        rows.append(row)
    return rows


def _coverage_sections(trace: dict | list | None) -> list[dict]:
    coverage = trace.get("coverage") if isinstance(trace, dict) else None
    if not isinstance(coverage, dict):
        return []
    sections = coverage.get("sections")
    if not isinstance(sections, list):
        return []
    return [item for item in sections if isinstance(item, dict) and item.get("title")]


def _chapter_for_paragraph(sections: list[dict], paragraph_index: int) -> str | None:
    if paragraph_index <= 0:
        return None
    for section in sections:
        span = _section_span(section)
        if span is None:
            continue
        start, end = span
        if start <= paragraph_index < end:
            return str(section["title"])
    return None


def _section_span(section: dict) -> tuple[int, int] | None:
    start = section.get("ordinal")
    if start is None:
        return None
    end = section.get("end")
    if end is None and section.get("n_paras") is not None:
        try:
            end = int(start) + int(section["n_paras"])
        except (TypeError, ValueError):
            return None
    if end is None:
        return None
    try:
        return int(start), int(end)
    except (TypeError, ValueError):
        return None


def _chapter_for_missed_section(sections: list[dict], text: str) -> str | None:
    needle = _normalize(text)
    if not needle:
        return None
    for section in sections:
        title = _normalize(str(section["title"]))
        if not title:
            continue
        if needle in title or title in needle:
            return str(section["title"])
    return None


def _normalize(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def _status_of(sections: list[dict], title: str) -> str:
    for section in sections:
        if str(section["title"]) == title:
            status = str(section.get("status") or "")
            return status if status in {"read", "probed", "unread"} else "unread"
    return ""


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _missed_dict(item: MissedIssue | dict) -> dict:
    if isinstance(item, MissedIssue):
        return item.to_dict()
    return dict(item)


def _token_usage(session) -> dict | None:
    raw = getattr(session, "token_usage", None)
    usage = dict(raw) if isinstance(raw, dict) else {}
    return usage or None


def _rate(part: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(part / total, 4)
