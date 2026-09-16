from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from thesis_review.applog import log_dir, setup, write_error, write_run
from thesis_review.cli import build_service, main as cli_main
from thesis_review.evalmetrics import compute_eval_metrics
from thesis_review.worker import main as worker_main
from thesis_review.fixtures import write_demo_drafts
from thesis_review.live import live_dir, read_progress, reset_live
from thesis_review.paths import app_home, gui_dir, path_is_file
from thesis_review.service import ThesisReviewService, session_stats, _load_trace
from thesis_review.session_store import SESSION_FAILED, ReviewSession
from thesis_review.settings import AppSettings, apply_model, load_settings, public_settings, save_settings
from thesis_review.types import Finding, derive_kind


class Bridge:
    def __init__(self, home: Path) -> None:
        self.home = home
        setup(home, kind="gui")
        self.service: ThesisReviewService = build_service(home)
        self.settings: AppSettings = load_settings(home)
        self.window = None
        self.reviewed_path = ""
        self.source_path = ""
        self.paper_path = ""
        self.output_dir = str(self.settings.output_dir or self._default_output())
        self.session: ReviewSession | None = None
        self.status = "准备就绪。"
        self.findings: list[dict] = []
        self.recall: dict = self._empty_recall()
        self.used_model = False
        self.warning = ""
        self._live_dir: Path | None = None
        self._reviewing = False
        self._review_done = False
        self._review_error = ""
        self._lock = threading.Lock()
        self._restore_session()

    @staticmethod
    def _empty_recall() -> dict:
        return {"confirmed": 0, "recalled": 0, "written": 0, "skipped": [], "absent": []}

    def state(self) -> dict:
        issues = [
            asdict(item)
            for item in self.service.store.list_issues(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
            )
        ]
        payload = public_settings(self.settings)
        session = self._session_payload()
        payload.update(
            {
                "issues": issues,
                "reviewed_path": self.reviewed_path,
                "source_path": self.source_path,
                "paper_path": self.paper_path or self.settings.last_paper_path,
                "output_dir": self.output_dir,
                "status": self.status,
                "findings": self.findings,
                "recall": self.recall,
                "used_model": self.used_model,
                "warning": self.warning,
                "session": session,
                "sessions": self.service.sessions.list_recent_briefs(teacher_id=self.settings.teacher_id, limit=8),
                "history_drafts": [asdict(item) for item in self.service.sessions.list_history_drafts(teacher_id=self.settings.teacher_id, student_id=self.settings.student_id)],
                "stats": session_stats([Finding.from_dict(item) if isinstance(item, dict) else item for item in self.findings]) if self.findings else session_stats([]),
                "eval_summary": self._eval_summary(),
                "stage": self._stage(),
                "log_dir": str(log_dir(self.home)),
            }
        )
        return payload

    def _eval_summary(self) -> dict | None:
        """V0.8 metrics panel data, derived live from the session and trace."""
        session = self.session
        if session is None:
            return None
        try:
            trace = {}
            if session.output_dir:
                trace_path = Path(session.output_dir) / f"{session.draft_id}-trace.json"
                if path_is_file(trace_path):
                    trace = _load_trace(Path(session.output_dir), session.draft_id)
            return compute_eval_metrics(session.findings, session.missed_issues, trace, session)
        except Exception as exc:  # noqa: BLE001 - eval panel must not freeze the GUI
            write_error(self.home, f"eval summary failed: {exc}")
            return None

    def save_identity(self, payload: dict) -> dict:
        self.settings.teacher_name = str(payload.get("teacher_name") or self.settings.teacher_name)
        self.settings.student_id = str(payload.get("student_id") or self.settings.student_id).strip()
        self.settings.student_name = str(payload.get("student_name") or self.settings.student_name)
        self.settings.major = str(payload.get("major") or self.settings.major)
        if not self.settings.teacher_id:
            self.settings.teacher_id = "teacher-a"
        save_settings(self.home, self.settings)
        self.status = "已保存身份。"
        return {"ok": True}

    def save_model(self, payload: dict) -> dict:
        apply_model(self.settings, payload)
        save_settings(self.home, self.settings)
        return {"ok": True, "api_key_set": bool(self.settings.api_key.strip())}

    def ingest_files(self) -> dict:
        files = self._pick(multiple=True)
        if not files:
            return {"ok": False, "message": "未选择文件。"}
        count = 0
        missing = []
        for index, path in enumerate(files, start=1):
            target = Path(path)
            if not target.is_file():
                missing.append(str(target))
                continue
            self.service.ingest_history(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                major=self.settings.major,
                draft_id=target.stem or f"history-{index}",
                data=target.read_bytes(),
                source_path=str(target),
            )
            count += 1
        self.status = f"已导入 {count} 份历史稿，问题已保存。历史稿只是辅助材料。"
        if missing:
            self.status += f" 有 {len(missing)} 个文件无法读取。"
        return {"ok": True, "message": self.status}

    def load_demo(self) -> dict:
        drafts = write_demo_drafts(self.home / "demo")
        self.settings.student_id = "zhou"
        self.settings.teacher_name = "老师甲"
        self.settings.major = "人工智能"
        save_settings(self.home, self.settings)
        for draft_id, path in drafts.items():
            if draft_id == "new":
                self.paper_path = str(path)
                self.settings.last_paper_path = str(path)
                continue
            self.service.ingest_history(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                major=self.settings.major,
                draft_id=draft_id,
                data=path.read_bytes(),
                source_path=str(path),
            )
        save_settings(self.home, self.settings)
        self.status = "已载入演示稿。请确认需要复查的历史问题，再开始 AI 初审。"
        return {"ok": True, "message": self.status}

    def set_issue(self, issue_id: str, confirmed: bool) -> dict:
        if confirmed:
            self.service.confirm_issue(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                issue_id=issue_id,
            )
        else:
            self.service.disable_issue(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                issue_id=issue_id,
            )
        return {"ok": True}

    def choose_paper(self) -> dict:
        files = self._pick(multiple=False)
        if not files:
            return {"ok": False, "message": "未选择新稿。"}
        path = Path(files[0])
        self.paper_path = str(path)
        self.settings.last_paper_path = str(path)
        save_settings(self.home, self.settings)
        self.status = f"已选择当前新稿：{path.name}"
        return {"ok": True, "message": self.status, "paper_path": str(path)}

    def review_file(self) -> dict:
        with self._lock:
            if self._reviewing:
                return {"ok": False, "started": False, "message": "正在审查，请稍候。"}
        paper = self.paper_path or self.settings.last_paper_path
        files = [paper] if paper and path_is_file(paper) else self._pick(multiple=False)
        if not files:
            return {"ok": False, "started": False, "message": "未选择新稿。"}
        path = Path(files[0])
        if not path_is_file(path):
            return {"ok": False, "started": False, "message": f"找不到稿件：{path}。已经提取的历史问题仍保留。"}
        data = path.read_bytes()
        output_dir = self._ensure_output()
        live = live_dir(output_dir)
        session = self.service.start_session(
            teacher_id=self.settings.teacher_id,
            student_id=self.settings.student_id,
            major=self.settings.major,
            draft_id=path.stem or "new",
            paper_path=str(path),
            settings=self.settings,
            teacher_name=self.settings.teacher_name,
            student_name=self.settings.student_name,
            output_dir=str(output_dir),
        )
        with self._lock:
            if self._reviewing:
                return {"ok": False, "started": False, "message": "正在审查，请稍候。"}
            self._live_dir = live
            self._reviewing = True
            self._review_done = False
            self._review_error = ""
            self.findings = []
            self.recall = self._empty_recall()
            self.warning = ""
            self.used_model = False
            self.reviewed_path = ""
            self.paper_path = str(path)
            self.session = session
            self.status = "AI 正在初审…"
            self.output_dir = str(output_dir)
        reset_live(live)
        self.settings.last_paper_path = str(path)
        self.settings.last_session_id = session.id
        save_settings(self.home, self.settings)
        threading.Thread(
            target=self._run_review,
            args=(path, data, output_dir, session),
            daemon=True,
        ).start()
        return {"ok": True, "started": True, "message": "已开始 AI 初审。", "session_id": session.id}

    def progress(self) -> dict:
        with self._lock:
            live = self._live_dir
            done = self._review_done
            error = self._review_error
            reviewing = self._reviewing
            findings = list(self.findings)
            recall = self.recall
            used_model = self.used_model
            warning = self.warning
            reviewed_path = self.reviewed_path
            status = self.status
            output = self.output_dir
            paper_path = self.paper_path
            source_path = self.source_path
            session = self._session_payload()
            stage = self._stage()
        payload = read_progress(live)
        live_findings = payload.get("findings") or []
        if not done and live_findings:
            findings = self._merge_findings(live_findings, findings)
        message = payload.get("message") or status
        if done and status:
            message = status
        stats = session_stats([Finding.from_dict(item) for item in findings]) if findings else session_stats([])
        return {
            "ok": not bool(error),
            "started": reviewing or done,
            "done": done,
            "error": error,
            "message": message,
            "findings": findings,
            "tech_log": payload.get("tech_log") or [],
            "reviewed_path": reviewed_path,
            "source_path": source_path,
            "paper_path": paper_path,
            "output_dir": output,
            "used_model": used_model,
            "recall": recall,
            "warning": warning,
            "status": status,
            "session": session,
            "stats": stats,
            "eval_summary": self._eval_summary() if done else None,
            "stage": stage,
        }

    def decide_finding(
        self,
        finding_id: str,
        decision: str,
        edited_text: str = "",
        edited_new_text: str = "",
        rejection_reason: str = "",
        rejection_note: str = "",
    ) -> dict:
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可处理的审稿会话。"}
        try:
            finding = self.service.decide_finding(
                session_id=session_id,
                finding_id=finding_id,
                decision=decision,
                edited_text=edited_text,
                edited_new_text=edited_new_text,
                rejection_reason=rejection_reason,
                rejection_note=rejection_note,
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the teacher
            return {"ok": False, "message": str(exc)}
        self.session = self.service.sessions.get(session_id)
        self.findings = [item.to_dict() for item in self.session.findings]
        self._persist_session()
        return {"ok": True, "finding": finding.to_dict(), "stats": session_stats(self.session.findings)}

    def set_rejection_reason(self, finding_id: str, rejection_reason: str = "", rejection_note: str = "") -> dict:
        """Teacher annotates why a candidate was rejected; decision untouched."""
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可处理的审稿会话。"}
        try:
            finding = self.service.set_rejection_reason(
                session_id=session_id,
                finding_id=finding_id,
                rejection_reason=str(rejection_reason or ""),
                rejection_note=str(rejection_note or ""),
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the teacher
            return {"ok": False, "message": str(exc)}
        self.session = self.service.sessions.get(session_id)
        self.findings = [item.to_dict() for item in self.session.findings]
        self._persist_session()
        return {"ok": True, "finding": finding.to_dict(), "stats": session_stats(self.session.findings)}

    def add_missed_issue(self, problem: str = "", section: str = "", category: str = "", note: str = "") -> dict:
        """Teacher records an issue the AI pass missed; eval data only."""
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可处理的审稿会话。"}
        try:
            issue = self.service.add_missed_issue(
                session_id=session_id,
                problem=str(problem or ""),
                section=str(section or ""),
                category=str(category or ""),
                note=str(note or ""),
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the teacher
            return {"ok": False, "message": str(exc)}
        self.session = self.service.sessions.get(session_id)
        return {"ok": True, "issue": issue.to_dict()}

    def remove_missed_issue(self, issue_id: str = "") -> dict:
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可处理的审稿会话。"}
        self.service.remove_missed_issue(session_id=session_id, issue_id=str(issue_id or ""))
        self.session = self.service.sessions.get(session_id)
        return {"ok": True}

    def export_eval(self) -> dict:
        """Write review-eval.json and the two CSV views for this session."""
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可导出的审稿会话。"}
        try:
            return self.service.export_eval(session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - surface the reason to the teacher
            return {"ok": False, "message": str(exc)}

    def accept_format_batch(self) -> dict:
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可处理的审稿会话。"}
        self.service.accept_format_findings(session_id)
        self.session = self.service.sessions.get(session_id)
        self.findings = [item.to_dict() for item in self.session.findings]
        self._persist_session()
        return {"ok": True, "stats": session_stats(self.session.findings)}

    def export_final(self, allow_pending: bool = False) -> dict:
        session_id = self.session.id if self.session else self.settings.last_session_id
        if not session_id:
            return {"ok": False, "message": "没有可导出的审稿会话。"}
        result = self.service.export_final(session_id=session_id, allow_pending=bool(allow_pending))
        if result.get("ok"):
            self.reviewed_path = str(result.get("reviewed_path") or "")
            self.session = self.service.sessions.get(session_id)
            self.findings = [item.to_dict() for item in self.session.findings]
            self.status = f"已生成正式审稿稿件，共写入 {result.get('n_exported', 0)} 条老师认可意见。"
            if result.get("warning"):
                self.status += result["warning"]
            self._persist_session()
        return result

    def resume_session(self, session_id: str) -> dict:
        try:
            session = self.service.sessions.get(session_id)
        except KeyError:
            return {"ok": False, "message": "找不到该审稿会话。"}
        self.session = session
        self.findings = [item.to_dict() for item in session.findings]
        self.paper_path = session.paper_path
        self.source_path = session.source_path
        self.output_dir = session.output_dir or self.output_dir
        self.reviewed_path = session.final_output_path
        self.warning = ""
        self._review_done = session.status in {"awaiting_teacher", "completed"}
        self._reviewing = False
        self.recall = self._recall_from_session(session)
        self.status = "已恢复未完成的审稿会话。" if not session.completed else "已打开历史审稿会话。"
        self.settings.last_session_id = session.id
        self.settings.last_paper_path = session.paper_path
        save_settings(self.home, self.settings)
        return {"ok": True, "session": session.to_dict()}

    def _run_review(self, path: Path, data: bytes, output_dir: Path, session: ReviewSession) -> None:
        confirmed = self.service.store.list_issues(
            teacher_id=self.settings.teacher_id,
            student_id=self.settings.student_id,
            status="confirmed",
        )
        try:
            hits = self.service.search_history(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                data=data,
            )
        except Exception:  # noqa: BLE001 - recall snapshot is best-effort
            hits = []
        recalled_ids = {hit.issue_id for hit in hits}
        try:
            result = self.service.review(
                teacher_id=self.settings.teacher_id,
                student_id=self.settings.student_id,
                draft_id=path.stem or "new",
                data=data,
                output_dir=output_dir,
                use_model=bool(self.settings.api_key),
                settings=self.settings,
                session=session,
                paper_path=str(path),
            )
        except Exception as exc:  # noqa: BLE001 - surface to teachers
            write_error(self.home, "gui review crashed", exc=exc, paper=str(path.name))
            persisted: list[Finding] = []
            try:
                current = self.service.sessions.get(session.id)
                current.status = SESSION_FAILED
                self.service.sessions.save(current)
                persisted = list(current.findings)
                session = current
            except Exception:  # noqa: BLE001 - session write is best-effort
                session.status = SESSION_FAILED
            with self._lock:
                self.session = session
                self.status = f"审查失败：{exc}"
                self.findings = [item.to_dict() for item in persisted]
                self.recall = self._empty_recall()
                self.warning = ""
                self._review_error = str(exc)
                self._review_done = True
                self._reviewing = False
            self._persist_session()
            return
        written_ids = {item.issue_id for item in result.findings if item.issue_id}
        skipped = []
        for hit in hits:
            if hit.issue_id in written_ids:
                continue
            issue = self.service.store.get(hit.issue_id)
            if result.warning:
                reason = "模型审查未完成，按规则不把字符串命中写成复犯。"
            else:
                reason = "在新稿中召回了相似原文，但未判定为复犯（可能已修复或依据不足），未写入候选。"
            skipped.append(
                {
                    "issue_id": hit.issue_id,
                    "category": issue.category,
                    "problem": issue.problem or issue.original_text,
                    "original_text": issue.original_text,
                    "new_quote": hit.new_quote,
                    "reason": reason,
                }
            )
        absent = [
            {
                "issue_id": issue.id,
                "category": issue.category,
                "problem": issue.problem or issue.original_text,
                "original_text": issue.original_text,
                "reason": "本次未在新稿中发现对应原文（可能已改正）。",
            }
            for issue in confirmed
            if issue.id not in recalled_ids
        ]
        extra = result.warning or ""
        try:
            session = self.service.sessions.get(result.session_id)
        except KeyError:
            session = self.session
        with self._lock:
            self.recall = {
                "confirmed": len(confirmed),
                "recalled": len(recalled_ids),
                "written": len(written_ids),
                "skipped": skipped,
                "absent": absent,
            }
            self.findings = [item.to_dict() for item in result.findings]
            self.used_model = result.used_model
            self.warning = result.warning or ""
            self.source_path = result.source_path or str(output_dir / f"{path.stem}-source.docx")
            self.reviewed_path = ""
            self.output_dir = str(output_dir)
            self.session = session
            self.status = f"AI 初审完成，{len(result.findings)} 条候选待老师处理。{extra}".strip()
            self._review_error = ""
            self._review_done = True
            self._reviewing = False
        if session is not None:
            session.quality = dict(session.quality or {})
            session.quality["recall"] = self.recall
            self.service.sessions.save(session)
        self._persist_session()

    def open_reviewed(self) -> dict:
        if not self.reviewed_path:
            return {"ok": False, "message": "还没有正式审稿稿件。请先确认意见并生成。"}
        return self._open_path(self.reviewed_path)

    def open_folder(self) -> dict:
        if not self.output_dir:
            return {"ok": False, "message": "还没有结果文件夹。"}
        return self._open_path(self.output_dir)

    def open_logs(self) -> dict:
        path = log_dir(self.home)
        write_run(self.home, "open logs folder")
        return self._open_path(str(path))

    def _open_path(self, target: str) -> dict:
        path = Path(target)
        if not path.exists():
            return {"ok": False, "message": f"找不到文件：{target}"}
        try:
            starter = getattr(os, "startfile", None)
            if starter is not None:
                starter(str(path))
                return {"ok": True}
            opener = shutil.which("xdg-open") or shutil.which("open")
            if opener:
                subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return {"ok": True}
        except OSError as exc:
            return {"ok": False, "message": f"无法打开 Word 文件：{exc}"}
        return {"ok": False, "message": "当前系统无法直接打开 Word。请到结果文件夹手动打开正式稿。"}

    def _stage(self) -> str:
        if self._reviewing:
            return "reviewing"
        if self.session and self.session.final_output_path:
            return "export"
        if self.findings or (self.session and self.session.findings):
            return "decide"
        return "prepare"

    def _recall_from_session(self, session: ReviewSession | None) -> dict:
        if session is None:
            return self._empty_recall()
        raw = (session.quality or {}).get("recall")
        if not isinstance(raw, dict):
            return self._empty_recall()
        recall = self._empty_recall()
        recall.update({key: raw.get(key, recall[key]) for key in recall})
        return recall

    def _session_payload(self) -> dict | None:
        if self.session is None:
            return None
        session = self.session
        return {
            "id": session.id,
            "status": session.status,
            "completed": session.completed,
            "missed_issues": [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in (session.missed_issues or [])
            ],
            "stats": session_stats(session.findings),
        }

    def _merge_findings(self, live_findings: list[dict], known: list[dict]) -> list[dict]:
        by_id = {item.get("id"): item for item in known if item.get("id")}
        merged = []
        for item in live_findings:
            current = dict(item)
            prior = by_id.get(current.get("id"))
            if prior and prior.get("teacher_decision") and prior.get("teacher_decision") != "pending":
                current["teacher_decision"] = prior["teacher_decision"]
                current["teacher_final_text"] = prior.get("teacher_final_text") or ""
            current.setdefault("kind", derive_kind(current))
            current.setdefault("teacher_decision", "pending")
            merged.append(current)
        return merged

    def _restore_session(self) -> None:
        if self.settings.last_paper_path:
            self.paper_path = self.settings.last_paper_path
        if self.settings.last_output_dir:
            self.output_dir = self.settings.last_output_dir
        if self.settings.last_reviewed_path and path_is_file(self.settings.last_reviewed_path):
            self.reviewed_path = self.settings.last_reviewed_path
        session_id = self.settings.last_session_id
        if not session_id:
            return
        try:
            session = self.service.sessions.get(session_id)
        except KeyError:
            return
        self.session = session
        self.findings = [item.to_dict() for item in session.findings]
        self.source_path = session.source_path
        if session.paper_path:
            self.paper_path = session.paper_path
        if session.output_dir:
            self.output_dir = session.output_dir
        if session.final_output_path:
            self.reviewed_path = session.final_output_path
        self.recall = self._recall_from_session(session)
        if session.findings:
            self._review_done = True
            self.status = "已恢复上次未完成的审稿会话。" if not session.completed else "已恢复上次审稿会话。"

    def _persist_session(self) -> None:
        if self.session is not None:
            self.settings.last_session_id = self.session.id
            if self.session.paper_path:
                self.settings.last_paper_path = self.session.paper_path
            if self.session.final_output_path:
                self.settings.last_reviewed_path = self.session.final_output_path
            if self.session.output_dir:
                self.settings.last_output_dir = self.session.output_dir
        else:
            self.settings.last_reviewed_path = self.reviewed_path
            self.settings.last_output_dir = self.output_dir
        save_settings(self.home, self.settings)

    def _default_output(self) -> Path:
        if self.settings.output_dir:
            return Path(self.settings.output_dir)
        return Path.home() / "Documents" / "论文审改结果"

    def _ensure_output(self) -> Path:
        target = self._default_output()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _pick(self, *, multiple: bool) -> list[str]:
        import webview

        if self.window is None:
            return []
        dialog = getattr(webview, "FileDialog", None)
        mode = dialog.OPEN if dialog is not None else webview.OPEN_DIALOG
        selected = self.window.create_file_dialog(
            mode,
            allow_multiple=multiple,
            file_types=("Word 文档 (*.docx)",),
        )
        if not selected:
            return []
        return [str(item) for item in selected]


def start_gui() -> int:
    import webview

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    bridge = Bridge(home)
    html = gui_dir() / "ui.html"
    window = webview.create_window(
        "论文审稿助手",
        str(html),
        js_api=bridge,
        width=1180,
        height=820,
        min_size=(880, 640),
        easy_drag=False,
        text_select=True,
    )
    bridge.window = window
    webview.start()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        return worker_main(sys.argv[2:])
    if len(sys.argv) > 1:
        return cli_main(sys.argv[1:])
    return start_gui()


if __name__ == "__main__":
    raise SystemExit(main())
