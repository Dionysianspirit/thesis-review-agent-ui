from __future__ import annotations

from thesis_review.types import Finding, derive_kind, is_confirmed_recidivism

GATE_FAIL_CODES = frozenset({"quote_not_in_draft", "missing_quote", "missing_source", "issue_mismatch"})


def summarize_quality(
    findings: list[Finding],
    trace: dict | list | None = None,
    *,
    duration_s: float = 0,
    search_count: int = 0,
    unnecessary_search: int = 0,
    gate_rejects: int = 0,
    tool_failures: int = 0,
    model_failures: int = 0,
) -> dict:
    decisions = {"pending": 0, "accepted": 0, "rejected": 0, "edited_accepted": 0}
    kinds: dict[str, int] = {}
    subtypes: dict[str, int] = {}
    history_recall = 0
    history_recidivism = 0
    for item in findings:
        decision = item.teacher_decision or "pending"
        decisions[decision] = decisions.get(decision, 0) + 1
        kind = derive_kind(item)
        kinds[kind] = kinds.get(kind, 0) + 1
        if item.subtype:
            subtypes[item.subtype] = subtypes.get(item.subtype, 0) + 1
        if kind == "history":
            history_recall += 1
            if is_confirmed_recidivism(item):
                history_recidivism += 1
    total = len(findings)
    accepted = decisions.get("accepted", 0) + decisions.get("edited_accepted", 0)
    rejected = decisions.get("rejected", 0)
    ops = _ops(trace)
    if tool_failures == 0:
        tool_failures = sum(1 for item in _entries(trace) if item.get("ok") is False)
    return {
        "ai_candidates": total,
        "accepted": decisions.get("accepted", 0),
        "rejected": rejected,
        "edited": decisions.get("edited_accepted", 0),
        "pending": decisions.get("pending", 0),
        "accepted_rate": _rate(accepted, total),
        "rejected_rate": _rate(rejected, total),
        "content_types": subtypes,
        "kinds": kinds,
        "history_recall": history_recall,
        "history_recidivism": history_recidivism,
        "gate_rejects": gate_rejects or _gate_rejects(trace),
        "web_search_count": search_count or ops.count("web_search"),
        "unnecessary_search": unnecessary_search,
        "tool_failures": tool_failures,
        "model_failures": model_failures,
        "duration_s": round(duration_s, 2),
        "tool_ops": ops,
        **_chapter_coverage(trace),
    }


def _chapter_coverage(trace: dict | list | None) -> dict:
    """Chapter coverage lives in the trace written at commit time. Absent for
    offline rule runs, which have no agent navigation to measure."""
    coverage = trace.get("coverage") if isinstance(trace, dict) else None
    if not isinstance(coverage, dict) or not coverage.get("total"):
        return {}
    summary = {
        "covered": int(coverage.get("covered") or 0),
        "total": int(coverage.get("total") or 0),
    }
    uncovered = [str(item) for item in coverage.get("uncovered") or []]
    if uncovered:
        summary["uncovered"] = uncovered
    return {"chapter_coverage": summary}


def _rate(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part / total, 4)


def _entries(trace: dict | list | None) -> list[dict]:
    if isinstance(trace, dict):
        items = trace.get("ops") or []
    else:
        items = trace or []
    return [item for item in items if isinstance(item, dict)]


def _ops(trace: dict | list | None) -> list[str]:
    return [str(item.get("op") or "") for item in _entries(trace) if item.get("op")]


def _gate_rejects(trace: dict | list | None) -> int:
    count = 0
    for item in _entries(trace):
        if item.get("ok") is False and str(item.get("code") or "") in GATE_FAIL_CODES:
            count += 1
    return count
