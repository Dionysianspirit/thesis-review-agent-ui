from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

from thesis_review.applog import write_error, write_run
from thesis_review.checks.format import check_format
from thesis_review.checks.language import check_language
from thesis_review.comments import comment_body as _comment_body
from thesis_review.comments import export_accepted_docx
from thesis_review.errors import ReviewError
from thesis_review.history.ingest import issues_from_comments, issues_from_revisions, persist
from thesis_review.history.match import match_issue
from thesis_review.history.store import HistoryStore
from thesis_review.live import append_event, live_dir, reset_live, write_findings
from thesis_review.llm import model_available
from thesis_review.quality import summarize_quality
from thesis_review.session_store import (
    HistoryDraftRecord,
    ReviewSession,
    SessionStore,
    TeacherFeedback,
    model_snapshot,
    new_feedback_id,
    new_session_id,
    now_iso,
)
from thesis_review.settings import AppSettings, load_settings
from thesis_review.types import (
    DECISION_ACCEPTED,
    DECISION_EDITED,
    DECISION_PENDING,
    DECISION_REJECTED,
    Evidence,
    Finding,
    HistoryHit,
    IssueRecord,
    ReviewResult,
    derive_kind,
    is_exportable,
    prepare_candidate,
)
from thesis_review.word.adapter import OpenedDocument, WordAdapter

ASSISTANT_AUTHOR = "审改助手"
PI_TIMEOUT_SEC = 600


def _fallback_detail(exc: BaseException) -> str:
    text = str(exc).replace("\r", "\n")
    name = type(exc).__name__
    code = getattr(exc, "code", "")
    if name == "TimeoutExpired" or code == "pi_timeout" or "timed out after" in text:
        return "初审超过等待时间，已停止。"
    if "ECONNREFUSED" in text or "worker portfile" in text or "worker connect" in text:
        return "本地审稿服务未能连上，请再试一次。"
    if text.lstrip().startswith("Command '[") or "Command '[" in text[:40]:
        return "本地审稿进程异常结束。"
    first = next((line.strip() for line in text.splitlines() if line.strip()), "模型审查失败")
    if " at " in first:
        first = first.split(" at ", 1)[0].strip()
    return first[:180]


def offline_fallback_warning(exc: BaseException, *, semantic: bool, recovered: bool = False) -> str:
    detail = _fallback_detail(exc)
    if recovered:
        warning = f"模型初审未跑完。{detail}已保留已经形成的候选。"
    else:
        warning = f"模型审查未能完成，已改用离线规则。{detail}"
    if semantic:
        warning += " 历史问题未经确认，未写成复犯。"
    return warning


def load_partial_findings(live: Path) -> list[Finding]:
    path = Path(live) / "findings.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    findings: list[Finding] = []
    for item in raw:
        if isinstance(item, dict) and (item.get("id") or item.get("problem")):
            findings.append(Finding.from_dict(item))
    return findings


class ThesisReviewService:
    def __init__(
        self,
        *,
        store: HistoryStore,
        adapter: WordAdapter | None = None,
        home: Path,
        sessions: SessionStore | None = None,
    ) -> None:
        self.store = store
        self.adapter = adapter or WordAdapter()
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.sessions = sessions or SessionStore(self.home / "review-sessions.sqlite")

    def ingest_history(
        self,
        *,
        teacher_id: str,
        student_id: str,
        major: str,
        draft_id: str,
        data: bytes,
        source_path: str = "",
    ) -> list[IssueRecord]:
        opened = self.adapter.open_bytes(data)
        comments = issues_from_comments(
            teacher_id=teacher_id,
            student_id=student_id,
            major=major,
            draft_id=draft_id,
            comments=self.adapter.extract_comments(opened),
        )
        revisions = issues_from_revisions(
            teacher_id=teacher_id,
            student_id=student_id,
            major=major,
            draft_id=draft_id,
            revisions=self.adapter.extract_revisions(opened),
            assistant_author=ASSISTANT_AUTHOR,
        )
        records = persist(self.store, comments + revisions)
        if source_path:
            self.sessions.add_history_draft(
                HistoryDraftRecord(
                    id=uuid.uuid4().hex,
                    teacher_id=teacher_id,
                    student_id=student_id,
                    draft_id=draft_id,
                    path=source_path,
                    imported_at=now_iso(),
                    issue_count=len(records),
                )
            )
        return records

    def confirm_issue(self, *, teacher_id: str, student_id: str, issue_id: str) -> IssueRecord:
        return self.store.set_status(
            teacher_id=teacher_id,
            student_id=student_id,
            issue_id=issue_id,
            status="confirmed",
        )

    def disable_issue(self, *, teacher_id: str, student_id: str, issue_id: str) -> IssueRecord:
        return self.store.set_status(
            teacher_id=teacher_id,
            student_id=student_id,
            issue_id=issue_id,
            status="disabled",
        )

    def search_history(
        self,
        *,
        teacher_id: str,
        student_id: str,
        data: bytes,
        opened: OpenedDocument | None = None,
    ) -> list[HistoryHit]:
        opened = opened or self.adapter.open_bytes(data)
        paragraphs = self.adapter.list_paragraphs(opened)
        hits: list[HistoryHit] = []
        for issue in self.store.list_issues(
            teacher_id=teacher_id, student_id=student_id, status="confirmed"
        ):
            hit = match_issue(issue, paragraphs)
            if hit is not None:
                hits.append(hit)
        return hits

    def history_findings(
        self,
        *,
        teacher_id: str,
        student_id: str,
        data: bytes,
        draft_id: str,
        session_id: str = "",
        opened: OpenedDocument | None = None,
    ) -> list[Finding]:
        hits = self.search_history(
            teacher_id=teacher_id, student_id=student_id, data=data, opened=opened
        )
        findings = []
        for hit in hits:
            findings.append(
                prepare_candidate(_history_finding(hit, self.store.get(hit.issue_id), draft_id), session_id=session_id)
            )
        return findings

    def start_session(
        self,
        *,
        teacher_id: str,
        student_id: str,
        major: str,
        draft_id: str,
        paper_path: str,
        settings: AppSettings | None = None,
        teacher_name: str = "",
        student_name: str = "",
        output_dir: str = "",
    ) -> ReviewSession:
        current = settings or load_settings(self.home)
        session = ReviewSession(
            id=new_session_id(),
            teacher_id=teacher_id,
            teacher_name=teacher_name or current.teacher_name,
            student_id=student_id,
            student_name=student_name or current.student_name,
            major=major,
            draft_id=draft_id,
            paper_path=paper_path,
            created_at=now_iso(),
            updated_at=now_iso(),
            status="reviewing",
            model_snapshot=model_snapshot(current),
            output_dir=output_dir,
        )
        return self.sessions.create(session)

    def review(
        self,
        *,
        teacher_id: str,
        student_id: str,
        draft_id: str,
        data: bytes,
        output_dir: Path,
        use_model: bool = False,
        settings: AppSettings | None = None,
        use_pi: bool = False,
        faux: bool = False,
        faux_scenario: str = "",
        pi_timeout: int = PI_TIMEOUT_SEC,
        offline_fallback: bool = True,
        session: ReviewSession | None = None,
        paper_path: str = "",
    ) -> ReviewResult:
        current = settings or load_settings(self.home)
        warning = ""
        semantic = use_model and model_available(current)
        should_pi = faux or use_pi or semantic
        live = live_dir(output_dir)
        reset_live(live)
        started = time.monotonic()
        if session is None:
            session = self.start_session(
                teacher_id=teacher_id,
                student_id=student_id,
                major=current.major,
                draft_id=draft_id,
                paper_path=paper_path,
                settings=current,
                output_dir=str(output_dir),
            )
        else:
            session.status = "reviewing"
            self.sessions.save(session)
        write_run(
            self.home,
            "review start",
            session=session.id,
            draft=draft_id,
            pi=should_pi,
            faux=faux,
            model=semantic,
            timeout=pi_timeout,
        )
        if should_pi:
            try:
                result = self._review_with_pi(
                    teacher_id=teacher_id,
                    student_id=student_id,
                    draft_id=draft_id,
                    data=data,
                    output_dir=output_dir,
                    settings=current,
                    faux=faux,
                    faux_scenario=faux_scenario,
                    timeout=pi_timeout,
                    session_id=session.id,
                )
                return self._finish_review(
                    session=session,
                    result=result,
                    original=data,
                    output_dir=output_dir,
                    draft_id=draft_id,
                    started=started,
                    warning=warning,
                )
            except Exception as exc:  # noqa: BLE001 - fall back to offline rules
                if not offline_fallback:
                    raise
                recovered = load_partial_findings(live)
                warning = offline_fallback_warning(exc, semantic=semantic, recovered=bool(recovered))
                write_error(
                    self.home,
                    "review pi failed",
                    exc=exc,
                    session=session.id,
                    recovered=len(recovered),
                )
                write_run(
                    self.home,
                    "review fallback",
                    session=session.id,
                    recovered=len(recovered),
                    offline=not recovered,
                )
                if recovered:
                    return self._finish_partial_pi(
                        session=session,
                        findings=recovered,
                        original=data,
                        output_dir=output_dir,
                        draft_id=draft_id,
                        started=started,
                        warning=warning,
                        used_model=semantic,
                    )
        append_event(live, {"op": "open_draft", "ok": True})
        opened = self.adapter.open_bytes(data)
        paragraphs = self.adapter.list_paragraphs(opened)
        tables = self.adapter.list_tables(opened)
        findings: list[Finding] = []
        findings.extend(check_language(paragraphs, draft_id=draft_id))
        findings.extend(check_format(paragraphs, tables, draft_id=draft_id))
        findings = [prepare_candidate(item, session_id=session.id) for item in findings]
        append_event(live, {"op": "run_checks", "ok": True})
        write_findings(live, findings)
        if not (semantic and warning):
            findings.extend(
                self.history_findings(
                    teacher_id=teacher_id,
                    student_id=student_id,
                    data=data,
                    draft_id=draft_id,
                    session_id=session.id,
                    opened=opened,
                )
            )
            append_event(live, {"op": "get_history_candidates", "ok": True})
            write_findings(live, findings)
        used_model = False
        if use_model and not warning and not model_available(current):
            warning = "未配置模型密钥，已改用离线规则。"

        output_dir.mkdir(parents=True, exist_ok=True)
        source_path = output_dir / f"{draft_id}-source.docx"
        reviewed_path = output_dir / f"{draft_id}-reviewed.docx"
        findings_path = output_dir / f"{draft_id}-findings.json"
        source_path.write_bytes(data)
        reviewed_path.write_bytes(data)
        findings_path.write_text(
            json.dumps([item.to_dict() for item in findings], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        append_event(live, {"op": "commit_review", "ok": True})
        write_findings(live, findings)
        append_event(live, {"op": "done", "ok": True})
        result = ReviewResult(
            reviewed_path=reviewed_path,
            findings_path=findings_path,
            findings=findings,
            used_model=used_model,
            warning=warning,
            session_id=session.id,
            source_path=str(source_path),
        )
        return self._finish_review(
            session=session,
            result=result,
            original=data,
            output_dir=output_dir,
            draft_id=draft_id,
            started=started,
            warning=warning,
        )

    def _finish_partial_pi(
        self,
        *,
        session: ReviewSession,
        findings: list[Finding],
        original: bytes,
        output_dir: Path,
        draft_id: str,
        started: float,
        warning: str,
        used_model: bool,
    ) -> ReviewResult:
        live = live_dir(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        source_path = output_dir / f"{draft_id}-source.docx"
        reviewed_path = output_dir / f"{draft_id}-reviewed.docx"
        findings_path = output_dir / f"{draft_id}-findings.json"
        if not source_path.is_file():
            source_path.write_bytes(original)
        if not reviewed_path.is_file():
            reviewed_path.write_bytes(original)
        append_event(live, {"op": "commit_review", "ok": True})
        write_findings(live, findings)
        append_event(live, {"op": "done", "ok": True})
        result = ReviewResult(
            reviewed_path=reviewed_path,
            findings_path=findings_path,
            findings=findings,
            used_model=used_model,
            warning=warning,
            session_id=session.id,
            source_path=str(source_path),
        )
        return self._finish_review(
            session=session,
            result=result,
            original=original,
            output_dir=output_dir,
            draft_id=draft_id,
            started=started,
            warning=warning,
        )

    def _finish_review(
        self,
        *,
        session: ReviewSession,
        result: ReviewResult,
        original: bytes,
        output_dir: Path,
        draft_id: str,
        started: float,
        warning: str,
    ) -> ReviewResult:
        findings = [prepare_candidate(item, session_id=session.id) for item in result.findings]
        source_path = output_dir / f"{draft_id}-source.docx"
        if not source_path.is_file():
            source_path.write_bytes(original)
        reviewed_path = output_dir / f"{draft_id}-reviewed.docx"
        if not reviewed_path.is_file():
            reviewed_path.write_bytes(original)
        session.findings = findings
        session.source_path = str(source_path)
        session.output_dir = str(output_dir)
        session.status = "awaiting_teacher"
        session.quality = summarize_quality(
            findings,
            _load_trace(output_dir, draft_id),
            duration_s=time.monotonic() - started,
        )
        self.sessions.save(session)
        result.findings = findings
        result.session_id = session.id
        result.source_path = str(source_path)
        result.quality = session.quality
        result.warning = warning or result.warning
        result.findings_path.write_text(
            json.dumps([item.to_dict() for item in findings], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_run(
            self.home,
            "review done",
            session=session.id,
            findings=len(findings),
            warning=bool(result.warning),
        )
        return result

    def decide_finding(
        self,
        *,
        session_id: str,
        finding_id: str,
        decision: str,
        edited_text: str = "",
    ) -> Finding:
        if decision not in {DECISION_PENDING, DECISION_ACCEPTED, DECISION_REJECTED, DECISION_EDITED}:
            raise ReviewError("invalid_decision", "不支持的老师决定。")
        if decision == DECISION_EDITED and not edited_text.strip():
            raise ReviewError("invalid_decision", "编辑后确认需要提供老师最终文本。")
        finding = self.sessions.set_decision(
            session_id=session_id,
            finding_id=finding_id,
            decision=decision,
            edited_text=edited_text,
        )
        session = self.sessions.get(session_id)
        self.sessions.add_feedback(
            TeacherFeedback(
                id=new_feedback_id(),
                session_id=session_id,
                finding_id=finding.id,
                teacher_id=session.teacher_id,
                student_id=session.student_id,
                paper_path=session.paper_path,
                section=finding.section,
                decision=decision,
                original_payload={
                    "problem": finding.original_problem or finding.problem,
                    "rationale": finding.original_rationale or finding.rationale,
                    "quote": finding.quote,
                    "kind": derive_kind(finding),
                    "source": finding.source,
                    "evidence": [item.__dict__ for item in finding.evidence],
                },
                edited_text=finding.teacher_final_text,
                created_at=now_iso(),
                kind=derive_kind(finding),
                category=finding.category,
                problem=finding.original_problem or finding.problem,
            )
        )
        return finding

    def accept_format_findings(self, session_id: str) -> list[Finding]:
        session = self.sessions.get(session_id)
        updated: list[Finding] = []
        for item in session.findings:
            if derive_kind(item) == "format" and item.teacher_decision == DECISION_PENDING:
                self.decide_finding(session_id=session_id, finding_id=item.id, decision=DECISION_ACCEPTED)
                updated.append(item)
        return updated

    def export_final(
        self,
        *,
        session_id: str,
        allow_pending: bool = False,
        dest: Path | None = None,
    ) -> dict:
        session = self.sessions.get(session_id)
        pending = [item for item in session.findings if item.teacher_decision == DECISION_PENDING]
        if pending and not allow_pending:
            return {
                "ok": False,
                "needs_confirm": True,
                "pending": len(pending),
                "message": f"仍有 {len(pending)} 条意见未处理。可继续处理，或仅使用当前已确认意见生成。",
                "stats": session_stats(session.findings),
            }
        original_path = Path(session.source_path or session.paper_path)
        if not original_path.is_file():
            raise ReviewError("open_failed", "找不到本稿文件，无法生成正式审稿稿件。")
        original = original_path.read_bytes()
        output_dir = Path(session.output_dir or original_path.parent)
        output_dir.mkdir(parents=True, exist_ok=True)
        reviewed_path = dest or (output_dir / f"{session.draft_id}-reviewed.docx")
        export_accepted_docx(
            adapter=self.adapter,
            original=original,
            findings=session.findings,
            dest=reviewed_path,
        )
        session.final_output_path = str(reviewed_path)
        session.completed = True
        session.status = "completed"
        session.quality = summarize_quality(session.findings, _load_trace(output_dir, session.draft_id))
        self.sessions.save(session)
        exported = [item for item in session.findings if is_exportable(item)]
        warning = ""
        if pending and allow_pending:
            warning = f"已忽略 {len(pending)} 条未处理意见，仅写入老师已确认内容。"
        return {
            "ok": True,
            "reviewed_path": str(reviewed_path),
            "n_exported": len(exported),
            "pending": len(pending),
            "warning": warning,
            "stats": session_stats(session.findings),
            "quality": session.quality,
        }

    def _review_with_pi(
        self,
        *,
        teacher_id: str,
        student_id: str,
        draft_id: str,
        data: bytes,
        output_dir: Path,
        settings: AppSettings,
        faux: bool,
        faux_scenario: str = "",
        timeout: int = PI_TIMEOUT_SEC,
        session_id: str = "",
    ) -> ReviewResult:
        from thesis_review.runtime import python_path, run_pi_review

        output_dir.mkdir(parents=True, exist_ok=True)
        source = output_dir / f"{draft_id}-source.docx"
        source.write_bytes(data)
        payload = run_pi_review(
            {
                "home": str(self.home),
                "teacher_id": teacher_id,
                "student_id": student_id,
                "major": settings.major,
                "draft_path": str(source),
                "draft_id": draft_id,
                "output_dir": str(output_dir),
                "provider": settings.provider,
                "model": settings.model or "gpt-4o-mini",
                "api_key": settings.api_key,
                "base_url": settings.base_url,
                "python": sys.executable,
                "pythonpath": python_path(),
                "faux_scenario": faux_scenario,
                "session_id": session_id,
            },
            faux=faux,
            timeout=timeout,
        )
        findings_path = Path(payload["findings_path"])
        raw = json.loads(findings_path.read_text(encoding="utf-8"))
        findings = [Finding.from_dict(item) for item in raw]
        return ReviewResult(
            reviewed_path=Path(payload["reviewed_path"]),
            findings_path=findings_path,
            findings=findings,
            used_model=not faux,
            warning="",
            session_id=session_id,
            source_path=str(source),
        )


def session_stats(findings: list) -> dict:
    pending = accepted = edited = rejected = 0
    for raw in findings:
        item = raw if isinstance(raw, Finding) else Finding.from_dict(raw if isinstance(raw, dict) else {})
        if item.teacher_decision == DECISION_ACCEPTED:
            accepted += 1
        elif item.teacher_decision == DECISION_EDITED:
            edited += 1
        elif item.teacher_decision == DECISION_REJECTED:
            rejected += 1
        else:
            pending += 1
    return {
        "ai_candidates": len(findings),
        "accepted": accepted,
        "edited_accepted": edited,
        "rejected": rejected,
        "pending": pending,
        "formal": accepted + edited,
    }


def _load_trace(output_dir: Path, draft_id: str) -> dict:
    path = Path(output_dir) / f"{draft_id}-trace.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _history_finding(
    hit: HistoryHit,
    issue: IssueRecord,
    draft_id: str,
    *,
    confirmed: bool = False,
) -> Finding:
    if confirmed:
        recalled = (issue.problem or issue.original_text or "").strip().replace("\n", " ")
        problem = "学生在新稿中仍出现已确认的历史问题。"
        if recalled:
            problem = f"学生在新稿中仍出现已确认的历史问题：{recalled}"
        rationale = f"历次稿件已指出：{issue.original_text}"
        suggested = "请对照旧稿批注意图修改，并补上可核验的依据。"
    else:
        recalled = (issue.problem or issue.original_text or "").strip().replace("\n", " ")
        if len(recalled) > 60:
            recalled = recalled[:60].rstrip() + "…"
        if recalled:
            problem = f"历史召回：{recalled}。新稿出现相似原文，待老师判断是否复犯。"
        else:
            problem = "历史召回：新稿出现与已确认历史问题相似的原文，待老师判断是否复犯。"
        rationale = f"仅召回相似原文，未经模型确认，不能直接写成复犯。旧稿批注：{issue.original_text}"
        suggested = "请老师判断是否仍是同一问题；未确认前不会写入正式学生稿。"
    return Finding(
        id=f"history-{hit.issue_id}",
        issue_id=hit.issue_id,
        category=issue.category,
        source="history",
        kind="history",
        problem=problem,
        rationale=rationale,
        quote=hit.new_quote,
        anchor=hit.new_anchor,
        paragraph_index=hit.paragraph_index,
        apply="comment",
        draft_id=draft_id,
        history_refs=[hit.issue_id],
        suggested_action=suggested,
        evidence=[
            Evidence(kind="history", draft_id=issue.source_draft_id, text=issue.original_text),
            Evidence(
                kind="history_span",
                draft_id=issue.source_draft_id,
                text=issue.original_span or hit.original_span,
            ),
        ],
    )
