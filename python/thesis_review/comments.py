from __future__ import annotations

from thesis_review.errors import ReviewError
from thesis_review.types import ExportReport, Finding, is_exportable
from thesis_review.word.adapter import OpenedDocument, WordAdapter

ASSISTANT_AUTHOR = "审改助手"

# Why an approved write did not land, in words a teacher can act on.
WRITE_FAIL_ZH = {
    "anchor_stale": "该段原文与初审时不一致，定位已失效",
    "anchor_not_found": "找不到该段落",
    "anchor_invalid": "定位信息无效",
    "not_found": "要替换的原文不在该段中",
    "ambiguous_target": "要替换的原文在该段出现多次，无法确定位置",
    "no_paragraph": "没有可挂批注的正文段落",
    "nothing_to_write": "没有可写入 Word 的内容",
}
PART_ZH = {"comment": "批注", "revision": "修订"}


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


def _wants_comment(finding: Finding) -> bool:
    return finding.apply in {"comment", "both"}


def _wants_revision(finding: Finding) -> bool:
    return finding.apply in {"revision", "both"} and bool(finding.suggested_old and finding.suggested_new)


def apply_findings(
    adapter: WordAdapter,
    opened: OpenedDocument,
    findings: list[Finding],
    report: ExportReport | None = None,
) -> OpenedDocument:
    """Write only teacher-approved findings into the working copy.

    Every approved finding ends up either fully written or listed in
    ``report.not_written`` with the reason. A refused write is never
    swallowed: the teacher approved that opinion and has to hear that it
    did not reach the student's file.
    """
    report = report if report is not None else ExportReport()
    approved = [item for item in findings if is_exportable(item)]
    # Per finding: None = not asked for, True = written, False = refused.
    comment_done: dict[int, bool | None] = {}
    revision_done: dict[int, bool | None] = {}
    reasons: dict[int, list[tuple[str, str]]] = {}

    def refuse(index: int, part: str, code: str) -> None:
        reasons.setdefault(index, []).append((part, code))

    for index, finding in enumerate(approved):
        comment_done[index] = None
        if not _wants_comment(finding):
            continue
        comment_done[index] = False
        if not finding.anchor.startswith("P"):
            refuse(index, "comment", "no_paragraph")
            continue
        try:
            adapter.add_comment(
                opened,
                anchor=finding.anchor,
                text=comment_body(finding),
                author=ASSISTANT_AUTHOR,
            )
            comment_done[index] = True
        except ReviewError as exc:
            refuse(index, "comment", exc.code)

    # A tracked replacement changes the paragraph text, and with it the
    # paragraph's anchor. Later approved revisions on that same paragraph
    # still carry the anchor from the first pass, so they continue from the
    # anchor the engine handed back. Only paragraphs this export itself
    # changed are continued this way: an anchor that is stale for any other
    # reason stays a refusal.
    continued: dict[str, str] = {}
    for index, finding in enumerate(approved):
        revision_done[index] = None
        if not _wants_revision(finding):
            continue
        revision_done[index] = False
        final_new = finding.teacher_final_new.strip() if finding.teacher_final_new else ""
        try:
            result = adapter.replace_tracked(
                opened,
                anchor=continued.get(finding.anchor, finding.anchor),
                old=finding.suggested_old,
                new=final_new or finding.suggested_new,
                author=ASSISTANT_AUTHOR,
                # The finding is about this phrase in this paragraph, so every
                # occurrence inside the paragraph is revised.
                replace_all=True,
            )
            revision_done[index] = True
            if result.new_anchor:
                continued[finding.anchor] = result.new_anchor
        except ReviewError as exc:
            refuse(index, "revision", exc.code)

    report.approved += len(approved)
    for index, finding in enumerate(approved):
        asked = [done for done in (comment_done[index], revision_done[index]) if done is not None]
        if not asked:
            refuse(index, "comment", "nothing_to_write")
        if asked and all(asked):
            report.written += 1
            continue
        report.not_written.append(
            _not_written_entry(
                finding, comment_done[index], revision_done[index], reasons.get(index, [])
            )
        )
    return opened


def _not_written_entry(
    finding: Finding,
    comment: bool | None,
    revision: bool | None,
    reasons: list[tuple[str, str]],
) -> dict:
    missing = [part for part, done in (("comment", comment), ("revision", revision)) if done is False]
    if not missing:
        missing = ["comment"]
    reason = "；".join(
        f"{PART_ZH.get(part, part)}未写入：{WRITE_FAIL_ZH.get(code, code)}" for part, code in reasons
    )
    return {
        "id": finding.id,
        "problem": finding.teacher_final_text.strip() or finding.problem,
        "quote": finding.quote,
        "section": finding.section,
        "paragraph_index": finding.paragraph_index,
        "anchor": finding.anchor,
        "comment_written": comment is True,
        "revision_written": revision is True,
        "missing": missing,
        "codes": [code for _part, code in reasons],
        "reason": reason,
    }


def not_written_notice(report: ExportReport) -> str:
    """One teacher-facing sentence; empty when everything approved was written."""
    count = len(report.not_written)
    if not count:
        return ""
    partial = sum(1 for item in report.not_written if item["comment_written"] or item["revision_written"])
    detail = f"（其中 {partial} 条只写入了一部分）" if partial else ""
    return f"有 {count} 条老师认可的意见未能完整写入正式稿{detail}，请对照清单在 Word 中手动补上。"


def export_accepted_docx(
    *,
    adapter: WordAdapter,
    original: bytes,
    findings: list[Finding],
    dest,
) -> ExportReport:
    opened = adapter.open_bytes(original)
    report = ExportReport()
    apply_findings(adapter, opened, findings, report)
    adapter.save(opened, dest)
    return report
