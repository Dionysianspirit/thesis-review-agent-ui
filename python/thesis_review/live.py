from __future__ import annotations

import json
from pathlib import Path

ALLOWED_EVENT_KEYS = frozenset({"op", "ok", "heading", "code", "intent"})
HEADING_LIMIT = 80
INTENT_LIMIT = 120

OP_ZH = {
    "open_draft": "正在打开稿件",
    "list_outline": "正在读取论文结构",
    "read_paragraphs": "正在读正文",
    "find_text": "正在检索原文",
    "run_checks": "正在核对语言和格式",
    "get_history_candidates": "正在复查该学生以前出现过的问题",
    "semantic_history_candidates": "找到语义相似历史问题，正在确认是否真正复犯",
    "confirm_history_finding": "已形成一条候选审稿意见",
    "record_argument_finding": "已形成一条候选审稿意见",
    "record_content_finding": "已形成一条候选审稿意见",
    "record_external_finding": "已形成一条候选审稿意见",
    "web_search": "正在核对外部数据来源",
    "web_fetch": "正在阅读外部来源",
    "get_teacher_feedback": "正在参考老师以往反馈",
    "coverage_status": "正在核对章节覆盖情况",
    "commit_review": "初审候选已保存，等待老师处理",
    "done": "AI 初审完成，请老师处理候选意见",
    "report_intent": "",
}

LOG_ZH = {
    "open_draft": "打开稿件",
    "list_outline": "读取结构",
    "read_paragraphs": "读正文",
    "read_section": "读章节",
    "find_text": "检索原文",
    "run_checks": "核对语言和格式",
    "get_history_candidates": "复查历史问题",
    "semantic_history_candidates": "语义复查历史",
    "confirm_history_finding": "写历史候选",
    "record_argument_finding": "写论证候选",
    "record_content_finding": "写内容候选",
    "record_external_finding": "写外部核验候选",
    "web_search": "外部检索",
    "web_fetch": "阅读外部来源",
    "get_teacher_feedback": "参考老师反馈",
    "coverage_status": "核对章节覆盖",
    "commit_review": "保存候选",
    "done": "初审结束",
}

CODE_ZH = {
    "quote_not_in_draft": "原文对不上",
    "missing_quote": "缺少原文",
    "invalid_params": "参数不完整",
    "nav_budget": "阅读次数已用尽",
    "not_open": "尚未打开稿件",
    "search_budget": "检索次数已用尽",
    "issue_mismatch": "历史记录对不上",
    "missing_source": "缺少来源",
    "section_unread": "章节未实际阅读",
}


def live_dir(output_dir: Path | str) -> Path:
    return Path(output_dir) / "live"


def reset_live(directory: Path | None) -> None:
    if directory is None:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    (path / "events.jsonl").write_text("", encoding="utf-8")
    (path / "findings.json").write_text("[]", encoding="utf-8")


def chinese_message(op: str, heading: str = "", previous: str = "", intent: str = "") -> str:
    note = str(intent or "").strip()
    if note:
        return note
    if op == "read_section":
        title = str(heading or "").strip()
        if title:
            return f"正在读：{title}"
        return "正在读章节"
    mapped = OP_ZH.get(op)
    if mapped is None:
        return previous
    if mapped == "":
        return previous
    return mapped


def append_event(directory: Path | None, event: dict) -> None:
    if directory is None:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    safe: dict = {}
    if "op" in event:
        safe["op"] = str(event.get("op") or "")
    if "ok" in event:
        safe["ok"] = bool(event.get("ok"))
    heading = str(event.get("heading") or "").strip()[:HEADING_LIMIT]
    if heading:
        safe["heading"] = heading
    intent = str(event.get("intent") or "").strip()[:INTENT_LIMIT]
    if intent:
        safe["intent"] = intent
    if event.get("code"):
        safe["code"] = str(event.get("code"))
    with (path / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        handle.flush()


def write_findings(directory: Path | None, findings: list) -> None:
    if directory is None:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    payload = [item.to_dict() if hasattr(item, "to_dict") else item for item in findings]
    (path / "findings.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_progress(directory: Path | None) -> dict:
    empty = {"message": "", "findings": [], "tech_log": []}
    if directory is None:
        return dict(empty)
    path = Path(directory)
    events_path = path / "events.jsonl"
    findings_path = path / "findings.json"
    events: list[dict] = []
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    findings: list = []
    if findings_path.is_file():
        try:
            raw = json.loads(findings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = []
        if isinstance(raw, list):
            findings = raw
    message = ""
    tech_log: list[dict] = []
    for event in events:
        op = str(event.get("op") or "")
        heading = str(event.get("heading") or "")
        intent = str(event.get("intent") or "")
        message = chinese_message(op, heading=heading, previous=message, intent=intent)
        if op == "report_intent":
            continue
        entry: dict = {"op": op, "ok": event.get("ok", True), "label": LOG_ZH.get(op, op)}
        if heading:
            entry["heading"] = heading
        if op != "report_intent" and intent:
            entry["intent"] = intent
        code = str(event.get("code") or "")
        if code:
            entry["code"] = code
            reason = CODE_ZH.get(code, "")
            if reason:
                entry["reason"] = reason
        tech_log.append(entry)
    return {"message": message, "findings": findings, "tech_log": tech_log}
