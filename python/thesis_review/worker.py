from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import uuid
from pathlib import Path

from thesis_review.applog import setup, write_error, write_op, write_run
from thesis_review.checks.format import check_format
from thesis_review.checks.language import check_language
from thesis_review.cli import build_service
from thesis_review.errors import ReviewError
from thesis_review.history.match import HEADING_RE, match_issue, normalize
from thesis_review.history.semantic import hybrid_recall
from thesis_review.live import append_event, reset_live, write_findings
from thesis_review.quality import GATE_FAIL_CODES
from thesis_review.session_store import feedback_as_soft_reference
from thesis_review.service import _history_finding
from thesis_review.types import (
    CONTENT_SUBTYPES,
    Evidence,
    ExternalSource,
    Finding,
    HistoryHit,
    IssueRecord,
    ParagraphView,
    prepare_candidate,
)
from thesis_review.web import WebSearcher
from thesis_review.word.adapter import OpenedDocument, WordAdapter

NAV_OPS = frozenset({"list_outline", "read_section", "read_paragraphs", "find_text"})
LIVE_FINDING_OPS = frozenset(
    {
        "run_checks",
        "confirm_history_finding",
        "record_argument_finding",
        "record_content_finding",
        "record_external_finding",
    }
)
# Section coverage states, weakest to strongest. A section counts as covered
# only after a real read; find_text hits leave it "probed" (partial contact).
COVERAGE_UNREAD = "unread"
COVERAGE_PROBED = "probed"
COVERAGE_READ = "read"
COVERAGE_HINT_TITLES = 4
NAV_BUDGET = 20
NAV_BUDGET_MAX = 60
# Free-form kinds like「语言问题」 silently misroute category derivation, so
# only canonical kinds are accepted at the record boundary.
RECORD_KINDS = frozenset({"content", "language", "format", "external"})
# These subtypes conclude from cross-paragraph evidence; recording them from a
# find_text snippet alone (40 chars of context) is the main false-positive
# source, so the claim's section must have actually been read first.
HIGH_RISK_SUBTYPES = frozenset({"data_consistency", "method", "experiment"})
MIN_PROBLEM_CHARS = 6
SEARCH_BUDGET = 3
TRACE_PARAM_KEYS = frozenset({"start_ordinal", "limit", "max_hits", "draft_id", "issue_id", "subtype", "kind"})
MAX_READ_PARAS = 8
MAX_READ_CHARS = 2000
MAX_CONTENT_FINDINGS = 10
MAX_ARGUMENT_FINDINGS = MAX_CONTENT_FINDINGS
MAX_FIND_HITS = 5
OUTLINE_TEXT_LIMIT = 80
FIND_CONTEXT = 40
INTENT_LIMIT = 120
NAMED_HEADINGS = (
    "摘要",
    "绪论",
    "引言",
    "相关工作",
    "研究方法",
    "实验",
    "实验结果",
    "结果与分析",
    "结论",
    "参考文献",
)


class Worker:
    def __init__(
        self,
        *,
        home: Path,
        teacher_id: str,
        student_id: str,
        major: str,
        live: Path | None = None,
        session_id: str = "",
        searcher: WebSearcher | None = None,
        draft_id: str = "",
        draft_path: str = "",
        review_dir: Path | str | None = None,
    ) -> None:
        self.home = Path(home)
        self.teacher_id = teacher_id
        self.student_id = student_id
        self.major = major
        self.live = Path(live) if live else None
        self.session_id = session_id
        # Authoritative draft identity passed out-of-band by the parent. The
        # model relays these through tool params and normalizes characters
        # (e.g. curly quotes -> '"', illegal in Windows filenames), so params
        # are only a fallback when the parent did not supply the truth.
        self.draft_id = str(draft_id or "")
        self.draft_path = str(draft_path or "")
        self.review_dir = Path(review_dir) if review_dir else None
        self.service = build_service(self.home)
        self.sessions = self.service.sessions
        self.searcher = searcher or WebSearcher()
        self.adapter = WordAdapter()
        self.opened: OpenedDocument | None = None
        self.original: bytes = b""
        # Lazily-opened view of `self.original`; the review loop consults the
        # original text on every history confirm, so parse it at most once.
        self._original_opened: OpenedDocument | None = None
        self.findings: list[Finding] = []
        self.nav_calls = 0
        self.nav_budget = NAV_BUDGET
        self.search_calls = 0
        self.content_count = 0
        self.argument_count = 0
        self.gate_rejects = 0
        self.trace: list[dict] = []
        # Chapter coverage map: one entry per outline section plus the front
        # matter, each {ordinal, end, title, n_paras, status, reads}. Built on
        # open_draft; every read/find updates it so "review finished" can never
        # be mistaken for "every chapter was actually checked".
        self.sections: list[dict] = []
        # Prewarm handshake: the background parse of --draft-path publishes its
        # result under this lock, and op_open_draft consumes it under the same
        # lock, so an early open_draft just waits for the parse it would have
        # run itself. Guarded state: (original, opened, mtime_ns, size).
        self._prewarm_lock = threading.Lock()
        self._prewarmed: tuple[bytes, OpenedDocument, int, int] | None = None

    def dispatch(self, op: str, params: dict) -> dict:
        try:
            if op in NAV_OPS:
                if self.nav_calls >= self.nav_budget:
                    raise ReviewError(
                        "nav_budget",
                        f"导航次数已达上限（共 {self.nav_budget} 次），只能记录发现或提交审改。{self._uncovered_hint()}",
                    )
                self.nav_calls += 1
            handler = getattr(self, f"op_{op}", None)
            if handler is None:
                raise ReviewError("unknown_op", f"未知操作：{op}")
            result = handler(params)
            self._record_trace(op, params, ok=True)
            self._emit_live(op, params, ok=True)
            if op in LIVE_FINDING_OPS:
                self._write_live_findings()
            if op == "commit_review":
                result = dict(result)
                result["trace_path"] = self._write_trace(params)
                coverage = self.coverage_summary()
                result["coverage"] = coverage
                self._write_live_findings()
                self._emit_live("coverage", {}, ok=True, intent=_coverage_message(coverage))
                self._emit_live("done", {}, ok=True)
            write_op(
                self.home,
                op,
                ok=True,
                session=self.session_id,
                **_safe_trace_params(params),
            )
            return result
        except ReviewError as exc:
            if exc.code in GATE_FAIL_CODES:
                self.gate_rejects += 1
            self._record_trace(op, params, ok=False, code=exc.code)
            self._emit_live(op, params, ok=False, code=exc.code)
            write_op(
                self.home,
                op,
                ok=False,
                code=exc.code,
                session=self.session_id,
                **_safe_trace_params(params),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - log then protocol-encode
            write_error(self.home, f"worker {op} crashed", exc=exc, session=self.session_id)
            raise

    def _record_trace(self, op: str, params: dict, *, ok: bool, code: str = "") -> None:
        entry: dict = {"op": op, "ok": ok, "params": _safe_trace_params(params)}
        if not ok:
            entry["code"] = code
        self.trace.append(entry)

    def _write_trace(self, params: dict) -> str:
        output_dir, draft_id = self._commit_target(params)
        output_dir.mkdir(parents=True, exist_ok=True)
        trace_path = output_dir / f"{draft_id}-trace.json"
        payload = {
            "draft_id": draft_id,
            "session_id": self.session_id,
            "ops": self.trace,
            "gate_rejects": self.gate_rejects,
            "search_calls": self.search_calls,
            "coverage": self.coverage_summary(),
        }
        trace_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(trace_path)

    def _emit_live(self, op: str, params: dict, *, ok: bool, code: str = "", intent: str = "") -> None:
        event: dict = {"op": op, "ok": ok}
        heading = self._live_heading(op, params)
        if heading:
            event["heading"] = heading
        if op == "report_intent":
            intent = str(params.get("message") or params.get("intent") or "").strip()
        if intent:
            event["intent"] = intent.strip()[:INTENT_LIMIT]
        if code:
            event["code"] = code
        append_event(self.live, event)

    def _write_live_findings(self) -> None:
        write_findings(self.live, self.findings)
        if self.session_id:
            try:
                self.sessions.replace_findings(self.session_id, self.findings)
            except KeyError:
                pass

    def _live_heading(self, op: str, params: dict) -> str:
        if op != "read_section" or "start_ordinal" not in params:
            return ""
        try:
            ordinal = int(params["start_ordinal"])
        except (TypeError, ValueError):
            return ""
        try:
            items = self._paragraphs()
        except ReviewError:
            return ""
        for item in items:
            if item.ordinal == ordinal:
                return item.text[:OUTLINE_TEXT_LIMIT]
        return ""

    def prewarm(self) -> None:
        """Parse --draft-path in the background before the agent connects.

        The result is only a cache of pure reads: workers never mutate the
        draft, so the parsed document stays valid for the first open_draft.
        Any failure is swallowed — without a prewarm the worker behaves exactly
        as before, parsing on the first open_draft call.
        """
        if not self.draft_path:
            return
        try:
            path = Path(self.draft_path)
            stat = path.stat()
            data = path.read_bytes()
            opened = self.adapter.open_bytes(data)
            # Parse (and cache) the paragraph index inside the lock so a
            # consumer can never observe a half-prewarmed entry.
            with self._prewarm_lock:
                self._prewarmed = (data, opened, stat.st_mtime_ns, stat.st_size)
                self.adapter.list_paragraphs(opened)
        # A failed prewarm must stay silent: open_draft re-parses on demand.
        except Exception:  # noqa: S110, BLE001
            pass

    def _take_prewarmed(self) -> tuple[bytes, OpenedDocument] | None:
        with self._prewarm_lock:
            prewarmed = self._prewarmed
            self._prewarmed = None
        if prewarmed is None:
            return None
        data, opened, mtime_ns, size = prewarmed
        try:
            stat = Path(self.draft_path).stat()
        except OSError:
            return None
        # Reuse only an unmodified file snapshot; anything else re-reads.
        if stat.st_mtime_ns != mtime_ns or stat.st_size != size:
            return None
        return data, opened

    def op_open_draft(self, params: dict) -> dict:
        if self.draft_path:
            prewarmed = self._take_prewarmed()
            if prewarmed is not None:
                data, opened = prewarmed
            else:
                data = Path(self.draft_path).read_bytes()
                opened = self.adapter.open_bytes(data)
        elif params.get("path"):
            data = Path(params["path"]).read_bytes()
            opened = self.adapter.open_bytes(data)
        else:
            import base64

            data = base64.b64decode(params["bytes_b64"])
            opened = self.adapter.open_bytes(data)
        self.original = data
        self.opened = opened
        self._original_opened = None
        self.findings = []
        self.nav_calls = 0
        self.nav_budget = NAV_BUDGET
        self.search_calls = 0
        self.content_count = 0
        self.argument_count = 0
        self.gate_rejects = 0
        self.trace = []
        reset_live(self.live)
        paragraphs = self.adapter.list_paragraphs(self.opened)
        self.nav_budget = _nav_budget_for(len(paragraphs))
        self._build_sections(paragraphs)
        return {"n_paragraphs": len(paragraphs), "nav_budget": self.nav_budget}

    def op_report_intent(self, params: dict) -> dict:
        message = str(params.get("message") or params.get("intent") or "").strip()
        if not message:
            raise ReviewError("invalid_params", "缺少检查意图。")
        return {"ok": True, "message": message[:INTENT_LIMIT]}

    def op_list_paragraphs(self, params: dict) -> dict:
        opened = self._require_open()
        return {
            "paragraphs": [
                {"ordinal": item.ordinal, "anchor": item.anchor, "text": item.text}
                for item in self.adapter.list_paragraphs(opened)
            ]
        }

    def op_run_checks(self, params: dict) -> dict:
        opened = self._require_open()
        draft_id = self._draft_id(params)
        findings = check_language(self.adapter.list_paragraphs(opened), draft_id=draft_id)
        findings.extend(
            check_format(self.adapter.list_paragraphs(opened), self.adapter.list_tables(opened), draft_id=draft_id)
        )
        prepared = [prepare_candidate(item, session_id=self.session_id) for item in findings]
        self.findings.extend(prepared)
        return {"findings": [item.to_dict() for item in prepared]}

    def op_get_history_candidates(self, params: dict) -> dict:
        self._require_open()
        hits = self.service.search_history(
            teacher_id=self.teacher_id,
            student_id=self.student_id,
            data=self.original,
            opened=self._original_doc(),
        )
        candidates = []
        for hit in hits:
            issue = self.service.store.get(hit.issue_id)
            candidates.append(_candidate_payload(hit, issue))
        return {"candidates": candidates}

    def op_semantic_history_candidates(self, params: dict) -> dict:
        self._require_open()
        issues = self.service.store.list_issues(
            teacher_id=self.teacher_id,
            student_id=self.student_id,
            status="confirmed",
        )
        hits = hybrid_recall(issues, self._original_paragraphs())
        return {
            "candidates": [
                {
                    "issue_id": hit.issue_id,
                    "new_quote": hit.new_quote,
                    "new_anchor": hit.new_anchor,
                    "paragraph_index": hit.paragraph_index,
                    "score": round(hit.score, 4),
                    "method": hit.method,
                    "original_text": hit.original_text,
                    "old_span": hit.original_span,
                    "problem": hit.problem,
                    "category": hit.category,
                    "needs_context_check": True,
                    "auto_recidivism": False,
                }
                for hit in hits
            ]
        }

    def op_get_teacher_feedback(self, params: dict) -> dict:
        items = self.sessions.list_feedback(
            teacher_id=self.teacher_id,
            student_id=self.student_id,
            limit=int(params.get("limit") or 12),
        )
        return {"items": [feedback_as_soft_reference(item) for item in items]}

    def op_confirm_history_finding(self, params: dict) -> dict:
        self._require_open()
        issue_id = str(params.get("issue_id") or "").strip()
        new_quote = str(params.get("new_quote") or params.get("claim") or "").strip()
        draft_id = self._draft_id(params)
        if not new_quote:
            raise ReviewError("missing_quote", "缺少新稿原文，未写入候选。")
        issue = self._confirmed_issue(issue_id)
        if not _history_text_on_record(issue.original_span, issue) and not self._quote_in_source_or_opened(
            issue.original_span
        ):
            raise ReviewError("issue_mismatch", "旧稿原文与记录对不上，未写入候选。")
        if not _history_text_on_record(issue.original_text, issue) and not self._quote_in_source_or_opened(
            issue.original_text
        ):
            raise ReviewError("issue_mismatch", "旧稿批注与记录对不上，未写入候选。")
        original_paras = self._original_paragraphs()
        source_para = _paragraph_with_quote(new_quote, original_paras)
        if source_para is None and self._paragraph_with_quote(new_quote) is None:
            raise ReviewError("quote_not_in_draft", "新稿原文不在稿件中，未写入候选。")
        hit = match_issue(issue, original_paras)
        semantic_ok = False
        if hit is None or new_quote not in hit.new_quote:
            semantic_ok = any(
                item.issue_id == issue.id and new_quote in item.new_quote
                for item in hybrid_recall([issue], original_paras, limit=8)
            )
            if not semantic_ok:
                raise ReviewError("quote_not_in_draft", "新稿原文与历史召回位置对不上，未写入候选。")
        quote_para = self._paragraph_with_quote(new_quote)
        if quote_para is None and hit is not None:
            quote_para = next((item for item in self._paragraphs() if item.ordinal == hit.paragraph_index), None)
        if quote_para is None:
            raise ReviewError("quote_not_in_draft", "新稿原文不在稿件中，未写入候选。")
        finding = prepare_candidate(
            _history_finding(
                HistoryHit(
                    issue_id=issue.id,
                    new_anchor=quote_para.anchor,
                    new_quote=new_quote,
                    paragraph_index=quote_para.ordinal,
                    evidence_text=issue.original_text,
                    original_span=issue.original_span or (hit.original_span if hit else ""),
                    category=issue.category,
                ),
                issue,
                draft_id,
                confirmed=True,
            ),
            session_id=self.session_id,
        )
        if params.get("rationale"):
            finding.rationale = str(params.get("rationale"))
            finding.original_rationale = finding.rationale
        self.findings.append(finding)
        return {"ok": True, "id": finding.id}

    def _confirmed_issue(self, issue_id: str) -> IssueRecord:
        if not issue_id:
            raise ReviewError("issue_mismatch", "缺少历史问题编号，未写入候选。")
        try:
            issue = self.service.store.get(issue_id)
        except KeyError as exc:
            raise ReviewError("issue_mismatch", "历史问题不存在，未写入候选。") from exc
        if (
            issue.teacher_id != self.teacher_id
            or issue.student_id != self.student_id
            or issue.status != "confirmed"
        ):
            raise ReviewError("issue_mismatch", "历史问题不属于当前师生或尚未确认，未写入候选。")
        return issue

    def op_list_outline(self, params: dict) -> dict:
        outline = []
        counts = {section["ordinal"]: section for section in self.sections}
        for item in self._paragraphs():
            if not _is_outline_heading(item.text):
                continue
            entry: dict = {
                "ordinal": item.ordinal,
                "anchor": item.anchor,
                "text": item.text[:OUTLINE_TEXT_LIMIT],
            }
            section = counts.get(item.ordinal)
            if section is not None:
                entry["n_paras"] = section["n_paras"]
            outline.append(entry)
        return {"outline": outline, "nav_left": self._nav_left()}

    def op_read_paragraphs(self, params: dict) -> dict:
        start = _require_ordinal(params)
        limit = int(params.get("limit") or MAX_READ_PARAS)
        selected = [item for item in self._paragraphs() if item.ordinal >= start]
        paragraphs, truncated = _clip_paragraphs(selected, limit=limit)
        self._mark_read(paragraphs)
        return {
            "paragraphs": paragraphs,
            "truncated": truncated,
            "nav_left": self._nav_left(),
            **_read_continuation(selected, paragraphs),
        }

    def op_read_section(self, params: dict) -> dict:
        start = _require_ordinal(params)
        limit = int(params.get("limit") or MAX_READ_PARAS)
        items = self._paragraphs()
        end = next(
            (
                item.ordinal
                for item in items
                if item.ordinal > start and _is_outline_heading(item.text)
            ),
            None,
        )
        selected = [
            item
            for item in items
            if item.ordinal >= start and (end is None or item.ordinal < end)
        ]
        paragraphs, truncated = _clip_paragraphs(selected, limit=limit)
        self._mark_read(paragraphs)
        return {
            "paragraphs": paragraphs,
            "truncated": truncated,
            "nav_left": self._nav_left(),
            **_read_continuation(selected, paragraphs),
        }

    def op_find_text(self, params: dict) -> dict:
        needle = str(params.get("needle") or "").strip()
        if not needle:
            raise ReviewError("invalid_params", "缺少检索词。")
        max_hits = min(int(params.get("max_hits") or MAX_FIND_HITS), MAX_FIND_HITS)
        hits: list[dict] = []
        for item in self._paragraphs():
            index = item.text.find(needle)
            if index < 0:
                continue
            start = max(0, index - FIND_CONTEXT)
            stop = min(len(item.text), index + len(needle) + FIND_CONTEXT)
            hits.append(
                {
                    "ordinal": item.ordinal,
                    "anchor": item.anchor,
                    "snippet": item.text[start:stop],
                }
            )
            self._mark_probed(item.ordinal)
            if len(hits) >= max_hits:
                break
        return {"hits": hits, "nav_left": self._nav_left()}

    def op_coverage_status(self, params: dict) -> dict:
        """In-memory coverage snapshot so the model can steer remaining budget."""
        sections = self._coverage_sections()
        uncovered = [item["title"] for item in sections if item["status"] != COVERAGE_READ]
        hint = ""
        if uncovered and self._nav_left() > 0:
            shown = "、".join(uncovered[:COVERAGE_HINT_TITLES])
            more = f" 等 {len(uncovered)} 章" if len(uncovered) > COVERAGE_HINT_TITLES else ""
            hint = f"尚有未覆盖章节：{shown}{more}。额度紧张时优先补齐这些章节。"
        return {
            "sections": sections,
            "uncovered": uncovered,
            "covered": len(sections) - len(uncovered),
            "total": len(sections),
            "nav_left": self._nav_left(),
            "hint": hint,
        }

    def op_web_search(self, params: dict) -> dict:
        if self.search_calls >= SEARCH_BUDGET:
            raise ReviewError("search_budget", "外部检索次数已达上限。")
        query = str(params.get("query") or params.get("needle") or "").strip()
        if not query:
            raise ReviewError("invalid_params", "缺少检索词。")
        try:
            hits = self.searcher.search(query, limit=int(params.get("limit") or 5))
        except ReviewError as exc:
            if exc.code == "search_failed":
                self.search_calls += 1
                return {"ok": False, "error": exc.message, "hits": [], "query": query, "fabricated": False}
            raise
        self.search_calls += 1
        return {
            "ok": True,
            "query": query,
            "hits": [item.to_dict() if hasattr(item, "to_dict") else item for item in hits],
            "fabricated": False,
        }

    def op_web_fetch(self, params: dict) -> dict:
        if self.search_calls >= SEARCH_BUDGET:
            raise ReviewError("search_budget", "外部检索次数已达上限。")
        url = str(params.get("url") or "").strip()
        try:
            result = self.searcher.fetch(url)
        except ReviewError as exc:
            if exc.code in {"search_failed", "invalid_params"}:
                self.search_calls += 1
                return {"ok": False, "error": exc.message, "url": url, "text": "", "fabricated": False}
            raise
        self.search_calls += 1
        return result

    def op_record_argument_finding(self, params: dict) -> dict:
        params = dict(params)
        params.setdefault("kind", "content")
        params.setdefault("subtype", "argument")
        params.setdefault("claim_quote", params.get("quote"))
        return self.op_record_content_finding(params)

    def op_record_external_finding(self, params: dict) -> dict:
        params = dict(params)
        params["kind"] = "external"
        params.setdefault("subtype", params.get("subtype") or "citation")
        return self.op_record_content_finding(params)

    def op_record_content_finding(self, params: dict) -> dict:
        kind = str(params.get("kind") or "content").strip() or "content"
        if kind not in RECORD_KINDS:
            raise ReviewError(
                "invalid_params",
                f"kind 只能是 {'/'.join(sorted(RECORD_KINDS))}，收到：{kind}。",
            )
        subtype = str(params.get("subtype") or "").strip()
        quote = str(params.get("quote") or params.get("claim_quote") or "").strip()
        evidence_quote = str(params.get("evidence_quote") or params.get("quote_b") or "").strip()
        problem = str(params.get("problem") or "").strip()
        rationale = str(params.get("rationale") or "").strip()
        draft_id = self._draft_id(params)
        section = str(params.get("section") or "").strip()
        suggested_action = str(params.get("suggested_action") or "").strip()
        if self.content_count >= MAX_CONTENT_FINDINGS:
            label = "外部核验" if kind == "external" else "内容发现"
            raise ReviewError(
                "argument_limit",
                f"{label}已达上限（内容与外部核验合计 {MAX_CONTENT_FINDINGS} 条），只能提交审改。",
            )
        if "再次" in f"{problem}\n{rationale}" or "屡次" in f"{problem}\n{rationale}":
            raise ReviewError("repeat_wording", "论证批注不能使用「再次」「屡次」。")
        if kind == "external":
            sources = _parse_external_sources(params)
            if not sources:
                raise ReviewError("missing_source", "外部核验缺少来源，未写入候选。")
        else:
            sources = _parse_external_sources(params)
        if not quote:
            raise ReviewError("quote_not_in_draft", "主张或证据原文不在稿件中，未写入候选。")
        claim_para = self._paragraph_with_quote(quote)
        if claim_para is None:
            raise ReviewError("quote_not_in_draft", "主张或证据原文不在稿件中，未写入候选。")
        evidence_para = self._paragraph_with_quote(evidence_quote) if evidence_quote else None
        needs_pair = subtype in {"argument", "data_consistency"} or bool(params.get("claim_quote")) or bool(params.get("evidence_quote"))
        if needs_pair:
            if not evidence_quote or evidence_para is None:
                raise ReviewError("quote_not_in_draft", "主张或证据原文不在稿件中，未写入候选。")
        elif evidence_quote and evidence_para is None:
            raise ReviewError("quote_not_in_draft", "对照证据原文不在稿件中，未写入候选。")
        # Quality gate (after evidence checks): reject lazy placeholder text.
        # A missing or generic problem is worse than no candidate at all.
        if len(_strip_punct(problem)) < MIN_PROBLEM_CHARS:
            raise ReviewError(
                "invalid_params",
                f"问题描述过于空泛（少于 {MIN_PROBLEM_CHARS} 个有效字符），请写明具体问题或放弃记录。",
            )
        if not rationale or problem.strip() == rationale.strip() or quote.strip() in {problem.strip(), rationale.strip()}:
            raise ReviewError(
                "invalid_params",
                "理由不能为空、不能与问题描述或原文相同，请写明判断依据。",
            )
        if kind == "content" and subtype in HIGH_RISK_SUBTYPES and not self._section_is_read(claim_para.ordinal):
            raise ReviewError(
                "section_unread",
                "该章节尚未实际阅读；先用 read_section 读完整上下文，再记录数据/方法/实验类问题。",
            )
        if subtype and subtype not in CONTENT_SUBTYPES and kind == "content":
            subtype = subtype or "argument"
        self.content_count += 1
        self.argument_count = self.content_count
        category = "B"
        if kind == "language":
            category = "A"
        elif kind == "format":
            category = "C"
        elif kind == "history":
            category = "D"
        evidence = [Evidence(kind="claim", draft_id=draft_id, text=quote, anchor=claim_para.anchor)]
        if evidence_quote:
            evidence.append(
                Evidence(
                    kind="evidence",
                    draft_id=draft_id,
                    text=evidence_quote,
                    anchor=evidence_para.anchor if evidence_para else "",
                )
            )
        source = "argument" if subtype == "argument" and kind == "content" else kind
        if kind == "content" and subtype == "argument":
            source = "argument"
        elif kind == "external":
            source = "external"
        elif kind == "history":
            source = "history"
        elif kind in {"language", "format"}:
            source = "rule"
        finding = prepare_candidate(
            Finding(
                id=f"{kind}-{self.content_count}-{uuid.uuid4().hex[:8]}",
                category=category,
                source=source,
                code=str(params.get("code") or subtype or kind),
                problem=problem,
                rationale=rationale,
                quote=quote,
                anchor=claim_para.anchor,
                paragraph_index=claim_para.ordinal,
                apply="comment",
                draft_id=draft_id,
                kind=kind,
                subtype=subtype,
                section=section,
                suggested_action=suggested_action,
                evidence=evidence,
                history_refs=[str(item) for item in (params.get("history_refs") or []) if item],
                external_sources=sources,
                issue_id=str(params.get("issue_id") or "") or None,
            ),
            session_id=self.session_id,
        )
        if kind == "content" and subtype == "argument":
            finding.id = f"argument-{self.content_count}"
            finding.code = str(params.get("code") or "claim_without_evidence")
            finding.source = "argument"
        self.findings.append(finding)
        return {"ok": True, "id": finding.id}

    def op_add_comment(self, params: dict) -> dict:
        raise ReviewError("teacher_gate", "候选意见不能直接写入 Word，请老师确认后再导出。")

    def op_replace_tracked(self, params: dict) -> dict:
        raise ReviewError("teacher_gate", "候选意见不能直接写入 Word，请老师确认后再导出。")

    def op_commit_review(self, params: dict) -> dict:
        self._require_open()
        output_dir, draft_id = self._commit_target(params)
        output_dir.mkdir(parents=True, exist_ok=True)
        source_path = output_dir / f"{draft_id}-source.docx"
        reviewed_path = output_dir / f"{draft_id}-reviewed.docx"
        findings_path = output_dir / f"{draft_id}-findings.json"
        source_path.write_bytes(self.original)
        # Clean copy only. Formal student Word is generated after teacher decisions.
        reviewed_path.write_bytes(self.original)
        findings_path.write_text(
            json.dumps([item.to_dict() for item in self.findings], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if self.session_id:
            try:
                session = self.sessions.get(self.session_id)
                session.findings = list(self.findings)
                session.source_path = str(source_path)
                session.output_dir = str(output_dir)
                session.status = "awaiting_teacher"
                self.sessions.save(session)
            except KeyError:
                pass
        return {
            "reviewed_path": str(reviewed_path),
            "source_path": str(source_path),
            "findings_path": str(findings_path),
            "n_findings": len(self.findings),
        }

    def _require_open(self) -> OpenedDocument:
        if self.opened is None:
            raise ReviewError("not_open", "尚未打开稿件。")
        return self.opened

    def _build_sections(self, paragraphs: list[ParagraphView]) -> None:
        """Split the draft into [heading, next heading) spans plus front matter."""
        marks = [item for item in paragraphs if _is_outline_heading(item.text)]
        sections: list[dict] = []
        if paragraphs and (not marks or marks[0].ordinal > paragraphs[0].ordinal):
            first_heading = marks[0].ordinal if marks else paragraphs[-1].ordinal + 1
            sections.append(_section_entry(paragraphs[0].ordinal, first_heading, "开篇"))
        for index, mark in enumerate(marks):
            end = marks[index + 1].ordinal if index + 1 < len(marks) else mark.ordinal + 1
            if index + 1 == len(marks) and paragraphs:
                end = paragraphs[-1].ordinal + 1
            sections.append(_section_entry(mark.ordinal, end, mark.text))
        self.sections = sections

    def _mark_read(self, paragraphs: list[dict]) -> None:
        for item in paragraphs:
            ordinal = int(item.get("ordinal") or 0)
            for section in self.sections:
                if section["ordinal"] <= ordinal < section["end"]:
                    section["status"] = COVERAGE_READ
                    section["reads"] += 1
                    break

    def _mark_probed(self, ordinal: int) -> None:
        for section in self.sections:
            if section["ordinal"] <= ordinal < section["end"]:
                if section["status"] == COVERAGE_UNREAD:
                    section["status"] = COVERAGE_PROBED
                return

    def _section_is_read(self, ordinal: int) -> bool:
        return any(
            section["ordinal"] <= ordinal < section["end"] and section["status"] == COVERAGE_READ
            for section in self.sections
        )

    def _coverage_sections(self) -> list[dict]:
        return [
            {
                "ordinal": section["ordinal"],
                "title": section["title"],
                "n_paras": section["n_paras"],
                "status": section["status"],
                "reads": section["reads"],
            }
            for section in self.sections
        ]

    def coverage_summary(self) -> dict:
        sections = self._coverage_sections()
        uncovered = [item["title"] for item in sections if item["status"] != COVERAGE_READ]
        probed = [item["title"] for item in sections if item["status"] == COVERAGE_PROBED]
        return {
            "covered": len(sections) - len(uncovered),
            "total": len(sections),
            "uncovered": uncovered,
            "probed": probed,
            "sections": sections,
        }

    def _uncovered_hint(self) -> str:
        """Named in the nav_budget rejection so the log shows what was skipped."""
        uncovered = [
            section["title"]
            for section in self.sections
            if section["status"] == COVERAGE_UNREAD
        ]
        if not uncovered:
            return ""
        shown = "、".join(uncovered[:COVERAGE_HINT_TITLES])
        more = f" 等 {len(uncovered)} 章" if len(uncovered) > COVERAGE_HINT_TITLES else ""
        return f"本次未覆盖章节（供老师参考）：{shown}{more}。"

    def _nav_left(self) -> int:
        return max(0, self.nav_budget - self.nav_calls)

    def _draft_id(self, params: dict) -> str:
        """Authoritative draft id from the parent, else the model-supplied one."""
        return self.draft_id or str(params.get("draft_id") or "new")

    def _commit_target(self, params: dict) -> tuple[Path, str]:
        draft_id = self._draft_id(params)
        if self.review_dir is not None:
            return self.review_dir, draft_id
        raw_dir = str(params.get("output_dir") or "")
        if not raw_dir:
            raise ReviewError("invalid_params", "缺少输出目录，无法保存审改结果。")
        return Path(raw_dir), draft_id

    def _paragraphs(self) -> list[ParagraphView]:
        return self.adapter.list_paragraphs(self._require_open())

    def _original_paragraphs(self) -> list[ParagraphView]:
        return self.adapter.list_paragraphs(self._original_doc())

    def _original_doc(self) -> OpenedDocument:
        if self._original_opened is None:
            self._original_opened = self.adapter.open_bytes(self.original)
        return self._original_opened

    def _paragraph_with_quote(self, quote: str) -> ParagraphView | None:
        return _paragraph_with_quote(quote, self._paragraphs())

    def _quote_in_source_or_opened(self, quote: str) -> bool:
        if not quote:
            return True
        if _paragraph_with_quote(quote, self._original_paragraphs()) is not None:
            return True
        return self._paragraph_with_quote(quote) is not None


def _nav_budget_for(n_paragraphs: int) -> int:
    """A thesis longer than the default budget can cover must get more reads:
    NAV_BUDGET * MAX_READ_PARAS paragraphs is the floor, capped for cost."""
    full_reads = (n_paragraphs + MAX_READ_PARAS - 1) // MAX_READ_PARAS
    return max(NAV_BUDGET, min(NAV_BUDGET_MAX, full_reads))


def _section_entry(ordinal: int, end: int, title: str) -> dict:
    return {
        "ordinal": ordinal,
        "end": end,
        "title": str(title).strip()[:OUTLINE_TEXT_LIMIT] or f"P{ordinal}",
        "n_paras": max(0, end - ordinal),
        "status": COVERAGE_UNREAD,
        "reads": 0,
    }


def _read_continuation(selected: list[ParagraphView], paragraphs: list[dict]) -> dict:
    """Tell the model where a truncated read resumes, so it cannot mistake a
    partial section view for the whole section (premature conclusions)."""
    if not paragraphs:
        return {}
    last_ordinal = int(paragraphs[-1].get("ordinal") or 0)
    remaining = max(0, len(selected) - len(paragraphs))
    info: dict = {"next_ordinal": last_ordinal + 1}
    if remaining:
        info["remaining_paras"] = remaining
    return info


def _coverage_message(coverage: dict) -> str:
    """One-line teacher-readable coverage note for the live stream."""
    covered = int(coverage.get("covered") or 0)
    total = int(coverage.get("total") or 0)
    uncovered = [str(item) for item in coverage.get("uncovered") or []]
    text = f"章节覆盖 {covered}/{total}。"
    if uncovered:
        shown = "、".join(uncovered[:COVERAGE_HINT_TITLES])
        more = " 等" if len(uncovered) > COVERAGE_HINT_TITLES else ""
        text += f"未检查：{shown}{more}。"
    return text[:INTENT_LIMIT]


def _strip_punct(text: str) -> str:
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def _safe_trace_params(params: dict) -> dict:
    safe = {key: params[key] for key in TRACE_PARAM_KEYS if key in params}
    if "needle" in params:
        safe["needle_len"] = len(str(params.get("needle") or ""))
    if "query" in params:
        safe["query_len"] = len(str(params.get("query") or ""))
    return safe


def _paragraph_with_quote(quote: str, paragraphs: list[ParagraphView]) -> ParagraphView | None:
    if not quote:
        return None
    for item in paragraphs:
        if quote in item.text:
            return item
    return None


def _candidate_payload(hit: HistoryHit, issue: IssueRecord) -> dict:
    return {
        "issue_id": hit.issue_id,
        "category": issue.category,
        "problem": issue.problem or issue.original_text,
        "original_text": issue.original_text,
        "old_span": issue.original_span or hit.original_span,
        "new_quote": hit.new_quote,
        "new_anchor": hit.new_anchor,
        "issue_type": issue.issue_type,
        "scope": issue.scope,
        "teacher_intent": issue.teacher_intent,
        "expected_fix": issue.suggested_fix,
        "needs_context_check": True,
        "auto_recidivism": False,
    }


def _history_text_on_record(text: str, issue: IssueRecord) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    return value in {issue.original_span, issue.original_text, issue.problem}


def _parse_external_sources(params: dict) -> list[ExternalSource]:
    raw = params.get("external_sources") or params.get("sources") or []
    if isinstance(raw, dict):
        raw = [raw]
    sources: list[ExternalSource] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url and not title:
            continue
        sources.append(
            ExternalSource(
                title=title or url,
                url=url,
                source_type=str(item.get("source_type") or "web"),
                query=str(item.get("query") or params.get("query") or ""),
                checked_time=str(item.get("checked_time") or ""),
                snippet=str(item.get("snippet") or ""),
            )
        )
    return sources


def _is_outline_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if HEADING_RE.match(stripped) and len(normalize(stripped)) <= 40:
        return True
    compact = normalize(stripped)
    return compact in {normalize(name) for name in NAMED_HEADINGS}


def _require_ordinal(params: dict) -> int:
    if "start_ordinal" not in params:
        raise ReviewError("invalid_params", "缺少 start_ordinal。")
    return int(params["start_ordinal"])


def _clip_paragraphs(items: list[ParagraphView], *, limit: int) -> tuple[list[dict], bool]:
    cap = max(1, min(int(limit), MAX_READ_PARAS))
    selected: list[dict] = []
    chars = 0
    for item in items:
        if len(selected) >= cap or chars >= MAX_READ_CHARS:
            return selected, True
        text = item.text
        if chars + len(text) > MAX_READ_CHARS:
            remain = MAX_READ_CHARS - chars
            if remain > 0:
                selected.append(
                    {"ordinal": item.ordinal, "anchor": item.anchor, "text": text[:remain]}
                )
            return selected, True
        selected.append({"ordinal": item.ordinal, "anchor": item.anchor, "text": text})
        chars += len(text)
    return selected, False


def _handle_line(worker: Worker, line: str) -> str:
    request = json.loads(line)
    ident = request.get("id")
    try:
        result = worker.dispatch(str(request["op"]), request.get("params") or {})
        return json.dumps({"id": ident, "result": result}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - protocol boundary
        code = getattr(exc, "code", "error")
        return json.dumps({"id": ident, "error": {"code": code, "message": str(exc)}}, ensure_ascii=False)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="ascii")
    tmp.replace(path)


def _serve_tcp(worker: Worker, portfile: Path) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    _atomic_write_text(portfile, str(server.getsockname()[1]))
    try:
        conn, _unused = server.accept()
        with conn:
            buffer = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    conn.sendall((_handle_line(worker, line) + "\n").encode("utf-8"))
    finally:
        server.close()
        try:
            portfile.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thesis-review-worker")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--teacher", required=True)
    parser.add_argument("--student", required=True)
    parser.add_argument("--major", default="人工智能")
    parser.add_argument("--portfile", type=Path, default=None)
    parser.add_argument("--live", type=Path, default=None)
    parser.add_argument("--session", default="")
    parser.add_argument("--draft-id", default="")
    parser.add_argument("--draft-path", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    setup(args.home, kind="worker")
    write_run(
        args.home,
        "worker listen",
        teacher=args.teacher,
        student=args.student,
        session=args.session,
        portfile=str(args.portfile or ""),
    )
    worker = Worker(
        home=args.home,
        teacher_id=args.teacher,
        student_id=args.student,
        major=args.major,
        live=args.live,
        session_id=args.session,
        draft_id=args.draft_id,
        draft_path=args.draft_path,
        review_dir=args.output_dir,
    )
    if args.portfile is not None:
        # Overlap the draft parse (and the first ensure_engine import) with the
        # agent's connect handshake instead of serializing them before TTFR.
        threading.Thread(target=worker.prewarm, daemon=True).start()
        _serve_tcp(worker, args.portfile)
        return 0
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        sys.stdout.write(_handle_line(worker, line) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
