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
    quality = dict(getattr(session, "quality", None) or {})
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
            "duration_s": quality.get("duration_s"),
        },
        "model_snapshot": dict(getattr(session, "model_snapshot", None) or {}),
        "token_usage": _token_usage(session),
    }


def category_label(key: str) -> str:
    return KIND_LABELS.get(key, key)


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
        start = section.get("ordinal")
        end = section.get("end")
        if start is None or end is None:
            continue
        try:
            if int(start) <= paragraph_index < int(end):
                return str(section["title"])
        except (TypeError, ValueError):
            continue
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
