from __future__ import annotations

from thesis_review.errors import ReviewError
from thesis_review.types import Finding, is_exportable
from thesis_review.word.adapter import OpenedDocument, WordAdapter

ASSISTANT_AUTHOR = "审改助手"


def comment_body(finding: Finding) -> str:
    if finding.teacher_decision == "edited_accepted" and finding.teacher_final_text.strip():
        return finding.teacher_final_text.strip()
    lines = [finding.problem, finding.rationale]
    if finding.source == "history" or finding.kind == "history":
        comment = next((item.text for item in finding.evidence if item.kind == "history"), "")
        old_span = next((item.text for item in finding.evidence if item.kind == "history_span"), "")
        lines = [f"历次稿件已指出：{comment or finding.rationale}"]
        if old_span:
            lines.append(f"旧稿原文：「{old_span}」。")
        if finding.quote:
            lines.append(f"本稿对应位置：「{finding.quote}」。")
        lines.append("请对照修改，并补上可核验的依据。")
    elif finding.source in {"argument", "content"} or finding.kind in {"content", "external"}:
        evidence = next((item.text for item in finding.evidence if item.kind in {"evidence", "counter"}), "")
        lines = [finding.problem, finding.rationale]
        if finding.quote:
            label = "主张" if finding.subtype == "argument" else "论文原文"
            lines.append(f"{label}：「{finding.quote}」。")
        if evidence:
            lines.append(f"对照证据：「{evidence}」。")
        for extra in finding.evidence:
            if extra.kind in {"claim", "evidence", "counter", "history", "history_span"}:
                continue
            if extra.text:
                lines.append(f"{extra.kind}：「{extra.text}」。")
        for source in finding.external_sources:
            title = source.title or source.url
            lines.append(f"外部来源（仅供核对，非绝对结论）：{title} {source.url}".strip())
        if finding.suggested_action:
            lines.append(finding.suggested_action)
    elif finding.suggested_new:
        lines.append(f"建议将「{finding.suggested_old}」改为「{finding.suggested_new}」。")
    elif finding.suggested_action:
        lines.append(finding.suggested_action)
    return "\n".join(line for line in lines if line)


def apply_findings(adapter: WordAdapter, opened: OpenedDocument, findings: list[Finding]) -> OpenedDocument:
    """Write only teacher-approved findings into the working copy."""
    for finding in findings:
        if not is_exportable(finding):
            continue
        if finding.apply in {"comment", "both"} and finding.anchor.startswith("P"):
            try:
                adapter.add_comment(
                    opened,
                    anchor=finding.anchor,
                    text=comment_body(finding),
                    author=ASSISTANT_AUTHOR,
                )
            except ReviewError:
                continue
    for finding in findings:
        if not is_exportable(finding):
            continue
        if finding.apply in {"revision", "both"} and finding.suggested_old and finding.suggested_new:
            final_new = finding.teacher_final_new.strip() if finding.teacher_final_new else ""
            try:
                adapter.replace_tracked(
                    opened,
                    anchor=finding.anchor,
                    old=finding.suggested_old,
                    new=final_new or finding.suggested_new,
                    author=ASSISTANT_AUTHOR,
                )
            except ReviewError:
                continue
    return opened


def export_accepted_docx(
    *,
    adapter: WordAdapter,
    original: bytes,
    findings: list[Finding],
    dest,
) -> None:
    opened = adapter.open_bytes(original)
    apply_findings(adapter, opened, findings)
    adapter.save(opened, dest)
