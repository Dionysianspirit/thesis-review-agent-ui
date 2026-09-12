from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


DECISION_PENDING = "pending"
DECISION_ACCEPTED = "accepted"
DECISION_REJECTED = "rejected"
DECISION_EDITED = "edited_accepted"
FINAL_DECISIONS = frozenset({DECISION_ACCEPTED, DECISION_EDITED})
KIND_FORMAT = "format"
KIND_LANGUAGE = "language"
KIND_CONTENT = "content"
KIND_HISTORY = "history"
KIND_EXTERNAL = "external"
CONTENT_SUBTYPES = (
    "argument",
    "data_consistency",
    "method",
    "experiment",
    "structure",
    "terminology",
    "citation",
)


@dataclass(frozen=True)
class ParagraphView:
    ordinal: int
    anchor: str
    text: str


@dataclass(frozen=True)
class TableView:
    ordinal: int
    anchor: str
    previous_text: str
    previous_anchor: str = ""


@dataclass(frozen=True)
class CommentRecord:
    comment_id: str
    author: str
    text: str
    anchor: str
    span: str
    date: str = ""


@dataclass(frozen=True)
class RevisionRecord:
    revision_id: str
    kind: str
    text: str
    author: str
    date: str = ""


@dataclass
class ReplaceResult:
    n_replaced: int
    new_anchor: str
    new_text: str


@dataclass
class IssueRecord:
    id: str
    teacher_id: str
    student_id: str
    major: str
    source_draft_id: str
    category: str
    status: str
    original_kind: str
    original_text: str
    original_span: str
    original_context: str
    original_anchor: str
    suggested_fix: str = ""
    issue_type: str = ""
    problem: str = ""
    scope: str = ""
    teacher_intent: str = ""
    created_at: str = ""
    confirmed_at: str = ""


@dataclass
class HistoryHit:
    issue_id: str
    new_anchor: str
    new_quote: str
    paragraph_index: int
    evidence_text: str
    original_span: str
    category: str


@dataclass
class Evidence:
    kind: str
    draft_id: str
    text: str
    anchor: str = ""


@dataclass
class ExternalSource:
    title: str
    url: str
    source_type: str = "web"
    query: str = ""
    checked_time: str = ""
    snippet: str = ""


@dataclass
class Finding:
    id: str
    category: str
    source: str
    problem: str
    rationale: str
    quote: str
    anchor: str
    paragraph_index: int
    apply: str
    issue_id: str | None = None
    code: str = ""
    suggested_old: str | None = None
    suggested_new: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    draft_id: str = ""
    kind: str = ""
    subtype: str = ""
    section: str = ""
    suggested_action: str = ""
    history_refs: list[str] = field(default_factory=list)
    external_sources: list[ExternalSource] = field(default_factory=list)
    teacher_decision: str = DECISION_PENDING
    teacher_final_text: str = ""
    # Teacher-edited replacement text for the tracked revision, separate from
    # teacher_final_text (the comment body): editing one must not silently
    # change the other. Empty falls back to suggested_new.
    teacher_final_new: str = ""
    original_problem: str = ""
    original_rationale: str = ""
    session_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Finding":
        evidence = [
            Evidence(**_filter_fields(Evidence, item)) if isinstance(item, dict) else item
            for item in raw.get("evidence") or []
        ]
        external_sources = [
            ExternalSource(**_filter_fields(ExternalSource, item)) if isinstance(item, dict) else item
            for item in raw.get("external_sources") or []
        ]
        data: dict = {}
        defaults = {
            "id": "",
            "category": "",
            "source": "",
            "problem": "",
            "rationale": "",
            "quote": "",
            "anchor": "",
            "paragraph_index": 0,
            "apply": "comment",
        }
        for item in fields(cls):
            if item.name in {"evidence", "external_sources"}:
                continue
            if item.name in raw and raw[item.name] is not None:
                data[item.name] = raw[item.name]
            elif item.name in defaults and item.name not in raw:
                data[item.name] = defaults[item.name]
        data["evidence"] = evidence
        data["external_sources"] = external_sources
        return cls(**data)


@dataclass
class ReviewResult:
    reviewed_path: Path
    findings_path: Path
    findings: list[Finding]
    used_model: bool
    warning: str = ""
    session_id: str = ""
    source_path: str = ""
    quality: dict = field(default_factory=dict)


def derive_kind(finding: Finding | dict) -> str:
    if isinstance(finding, dict):
        kind = str(finding.get("kind") or "")
        source = str(finding.get("source") or "")
        category = str(finding.get("category") or "")
    else:
        kind = finding.kind
        source = finding.source
        category = finding.category
    if kind:
        return kind
    if source == "history":
        return KIND_HISTORY
    if source == "external":
        return KIND_EXTERNAL
    if source == "argument":
        return KIND_CONTENT
    if category == "C":
        return KIND_FORMAT
    if category == "A":
        return KIND_LANGUAGE
    if category == "B":
        return KIND_CONTENT
    return KIND_CONTENT


def is_exportable(finding: Finding) -> bool:
    return finding.teacher_decision in FINAL_DECISIONS


def is_confirmed_recidivism(finding: Finding) -> bool:
    """True only after Agent confirmed the same issue still exists, not mere recall."""
    if derive_kind(finding) != KIND_HISTORY:
        return False
    text = f"{finding.original_problem} {finding.problem}"
    return "仍出现已确认的历史问题" in text


def prepare_candidate(finding: Finding, *, session_id: str = "") -> Finding:
    if not finding.kind:
        finding.kind = derive_kind(finding)
    if not finding.teacher_decision:
        finding.teacher_decision = DECISION_PENDING
    if not finding.original_problem:
        finding.original_problem = finding.problem
    if not finding.original_rationale:
        finding.original_rationale = finding.rationale
    if session_id:
        finding.session_id = session_id
    if not finding.suggested_action and finding.suggested_old and finding.suggested_new:
        finding.suggested_action = f"将「{finding.suggested_old}」改为「{finding.suggested_new}」。"
    if finding.source == "argument" and not finding.subtype:
        finding.subtype = "argument"
    return finding


def _filter_fields(cls, raw: dict) -> dict:
    allowed = {item.name for item in fields(cls)}
    return {key: value for key, value in raw.items() if key in allowed}
