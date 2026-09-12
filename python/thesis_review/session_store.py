from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from thesis_review.types import Finding, derive_kind


SESSION_PREPARING = "preparing"
SESSION_REVIEWING = "reviewing"
SESSION_AWAITING = "awaiting_teacher"
SESSION_COMPLETED = "completed"
SESSION_FAILED = "failed"


@dataclass
class ReviewSession:
    id: str
    teacher_id: str
    student_id: str
    major: str
    draft_id: str
    paper_path: str
    created_at: str
    updated_at: str
    status: str = SESSION_PREPARING
    teacher_name: str = ""
    student_name: str = ""
    model_snapshot: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    final_output_path: str = ""
    completed: bool = False
    source_path: str = ""
    output_dir: str = ""
    quality: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["findings"] = [item.to_dict() if hasattr(item, "to_dict") else item for item in self.findings]
        payload["model_snapshot"] = _public_snapshot(self.model_snapshot)
        return payload


@dataclass
class HistoryDraftRecord:
    id: str
    teacher_id: str
    student_id: str
    draft_id: str
    path: str
    imported_at: str
    issue_count: int = 0
    accessible: bool = True


@dataclass
class TeacherFeedback:
    id: str
    session_id: str
    finding_id: str
    teacher_id: str
    student_id: str
    paper_path: str
    section: str
    decision: str
    original_payload: dict
    edited_text: str
    created_at: str
    kind: str = ""
    category: str = ""
    problem: str = ""


class SessionStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    teacher_id TEXT NOT NULL,
                    teacher_name TEXT NOT NULL DEFAULT '',
                    student_id TEXT NOT NULL,
                    student_name TEXT NOT NULL DEFAULT '',
                    major TEXT NOT NULL DEFAULT '',
                    draft_id TEXT NOT NULL DEFAULT '',
                    paper_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model_snapshot TEXT NOT NULL DEFAULT '{}',
                    findings TEXT NOT NULL DEFAULT '[]',
                    final_output_path TEXT NOT NULL DEFAULT '',
                    completed INTEGER NOT NULL DEFAULT 0,
                    source_path TEXT NOT NULL DEFAULT '',
                    output_dir TEXT NOT NULL DEFAULT '',
                    quality TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS history_drafts (
                    id TEXT PRIMARY KEY,
                    teacher_id TEXT NOT NULL,
                    student_id TEXT NOT NULL,
                    draft_id TEXT NOT NULL,
                    path TEXT NOT NULL DEFAULT '',
                    imported_at TEXT NOT NULL,
                    issue_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS teacher_feedback (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    teacher_id TEXT NOT NULL,
                    student_id TEXT NOT NULL,
                    paper_path TEXT NOT NULL DEFAULT '',
                    section TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL,
                    original_payload TEXT NOT NULL DEFAULT '{}',
                    edited_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    problem TEXT NOT NULL DEFAULT ''
                );
                """
            )
            self._conn.commit()

    def create(self, session: ReviewSession) -> ReviewSession:
        self.save(session)
        return session

    def save(self, session: ReviewSession) -> ReviewSession:
        session.updated_at = _now()
        session.model_snapshot = _public_snapshot(session.model_snapshot)
        payload = (
            session.id,
            session.teacher_id,
            session.teacher_name,
            session.student_id,
            session.student_name,
            session.major,
            session.draft_id,
            session.paper_path,
            session.created_at,
            session.updated_at,
            session.status,
            json.dumps(_public_snapshot(session.model_snapshot), ensure_ascii=False),
            json.dumps([item.to_dict() for item in session.findings], ensure_ascii=False),
            session.final_output_path,
            1 if session.completed else 0,
            session.source_path,
            session.output_dir,
            json.dumps(session.quality or {}, ensure_ascii=False),
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, teacher_id, teacher_name, student_id, student_name, major, draft_id,
                    paper_path, created_at, updated_at, status, model_snapshot, findings,
                    final_output_path, completed, source_path, output_dir, quality
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    teacher_id=excluded.teacher_id,
                    teacher_name=excluded.teacher_name,
                    student_id=excluded.student_id,
                    student_name=excluded.student_name,
                    major=excluded.major,
                    draft_id=excluded.draft_id,
                    paper_path=excluded.paper_path,
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    model_snapshot=excluded.model_snapshot,
                    findings=excluded.findings,
                    final_output_path=excluded.final_output_path,
                    completed=excluded.completed,
                    source_path=excluded.source_path,
                    output_dir=excluded.output_dir,
                    quality=excluded.quality
                """,
                payload,
            )
            self._conn.commit()
        return session

    def get(self, session_id: str) -> ReviewSession:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _row_to_session(row)

    def list_recent(self, *, teacher_id: str = "", limit: int = 8) -> list[ReviewSession]:
        sql = "SELECT * FROM sessions"
        params: list[object] = []
        if teacher_id:
            sql += " WHERE teacher_id=?"
            params.append(teacher_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_session(row) for row in rows]

    def latest_open(self, *, teacher_id: str) -> ReviewSession | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE teacher_id=? AND completed=0
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (teacher_id,),
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def replace_findings(self, session_id: str, findings: list[Finding]) -> ReviewSession:
        session = self.get(session_id)
        session.findings = [Finding.from_dict(item.to_dict()) if isinstance(item, Finding) else Finding.from_dict(item) for item in findings]
        return self.save(session)

    def upsert_finding(self, session_id: str, finding: Finding) -> ReviewSession:
        session = self.get(session_id)
        updated = False
        next_items: list[Finding] = []
        for item in session.findings:
            if item.id == finding.id:
                next_items.append(finding)
                updated = True
            else:
                next_items.append(item)
        if not updated:
            next_items.append(finding)
        session.findings = next_items
        return self.save(session)

    def set_decision(
        self,
        *,
        session_id: str,
        finding_id: str,
        decision: str,
        edited_text: str = "",
    ) -> Finding:
        if decision not in {"pending", "accepted", "rejected", "edited_accepted"}:
            raise ValueError(decision)
        session = self.get(session_id)
        found = None
        for item in session.findings:
            if item.id == finding_id:
                item.teacher_decision = decision
                if decision == "edited_accepted":
                    item.teacher_final_text = edited_text.strip()
                found = item
                break
        if found is None:
            raise KeyError(finding_id)
        self.save(session)
        return found

    def add_history_draft(self, record: HistoryDraftRecord) -> HistoryDraftRecord:
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT id FROM history_drafts
                WHERE teacher_id=? AND student_id=? AND draft_id=? AND path=?
                """,
                (record.teacher_id, record.student_id, record.draft_id, record.path),
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE history_drafts SET imported_at=?, issue_count=? WHERE id=?",
                    (record.imported_at, record.issue_count, existing["id"]),
                )
                self._conn.commit()
                record.id = existing["id"]
                return record
            self._conn.execute(
                """
                INSERT INTO history_drafts (
                    id, teacher_id, student_id, draft_id, path, imported_at, issue_count
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    record.id,
                    record.teacher_id,
                    record.student_id,
                    record.draft_id,
                    record.path,
                    record.imported_at,
                    record.issue_count,
                ),
            )
            self._conn.commit()
        return record

    def list_history_drafts(self, *, teacher_id: str, student_id: str) -> list[HistoryDraftRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM history_drafts
                WHERE teacher_id=? AND student_id=?
                ORDER BY imported_at DESC
                """,
                (teacher_id, student_id),
            ).fetchall()
        records = []
        for row in rows:
            path = str(row["path"] or "")
            records.append(
                HistoryDraftRecord(
                    id=row["id"],
                    teacher_id=row["teacher_id"],
                    student_id=row["student_id"],
                    draft_id=row["draft_id"],
                    path=path,
                    imported_at=row["imported_at"],
                    issue_count=int(row["issue_count"] or 0),
                    accessible=bool(path and Path(path).is_file()),
                )
            )
        return records

    def add_feedback(self, record: TeacherFeedback) -> TeacherFeedback:
        payload = json.dumps(_strip_secrets(record.original_payload), ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO teacher_feedback (
                    id, session_id, finding_id, teacher_id, student_id, paper_path, section,
                    decision, original_payload, edited_text, created_at, kind, category, problem
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.id,
                    record.session_id,
                    record.finding_id,
                    record.teacher_id,
                    record.student_id,
                    record.paper_path,
                    record.section,
                    record.decision,
                    payload,
                    record.edited_text,
                    record.created_at,
                    record.kind,
                    record.category,
                    record.problem,
                ),
            )
            self._conn.commit()
        return record

    def list_feedback(
        self,
        *,
        teacher_id: str,
        student_id: str = "",
        decision: str | None = None,
        limit: int = 20,
    ) -> list[TeacherFeedback]:
        sql = "SELECT * FROM teacher_feedback WHERE teacher_id=?"
        params: list[object] = [teacher_id]
        if student_id:
            sql += " AND student_id=?"
            params.append(student_id)
        if decision is not None:
            sql += " AND decision=?"
            params.append(decision)
        # rowid breaks ties within one second so "latest decision" is stable.
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_feedback(row) for row in rows]

    def list_feedback_final(
        self,
        *,
        teacher_id: str,
        student_id: str = "",
        limit: int = 20,
    ) -> list[TeacherFeedback]:
        """Final effective decision per finding, not the operation log.

        The full log stays in teacher_feedback; this view collapses repeats
        (accepted -> rejected etc.) to the latest row per finding so the soft
        reference never carries contradictory signals. Findings whose latest
        state went back to pending are dropped: an undecided finding is no
        signal either way.
        """
        rows = self.list_feedback(
            teacher_id=teacher_id,
            student_id=student_id,
            limit=max(limit * 4, 80),
        )
        latest: dict[tuple[str, str], TeacherFeedback] = {}
        for row in rows:  # DESC: first seen per key is the newest
            key = (row.session_id, row.finding_id)
            if key not in latest:
                latest[key] = row
        finals = [row for row in latest.values() if row.decision != "pending"]
        finals.sort(key=lambda row: row.created_at, reverse=True)
        return finals[:limit]


def new_session_id() -> str:
    return uuid.uuid4().hex


def new_feedback_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return _now()


def model_snapshot(settings) -> dict:
    return _public_snapshot(
        {
            "provider": getattr(settings, "provider", ""),
            "model": getattr(settings, "model", ""),
            "base_url": getattr(settings, "base_url", ""),
            "api_key_set": bool(str(getattr(settings, "api_key", "") or "").strip()),
            "reasoning": getattr(settings, "reasoning", "off") or "off",
        }
    )


def _public_snapshot(raw: dict | None) -> dict:
    data = dict(raw or {})
    data.pop("api_key", None)
    data.pop("THESIS_API_KEY", None)
    data.pop("OPENAI_API_KEY", None)
    if "api_key_set" not in data:
        data["api_key_set"] = False
    return data


def _strip_secrets(raw: dict | None) -> dict:
    data = dict(raw or {})
    for key in list(data):
        lowered = key.lower()
        if "key" in lowered or "secret" in lowered or "token" in lowered:
            data.pop(key, None)
    return data


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_to_session(row: sqlite3.Row) -> ReviewSession:
    raw_findings = json.loads(row["findings"] or "[]")
    findings = [Finding.from_dict(item) if isinstance(item, dict) else item for item in raw_findings]
    snapshot = json.loads(row["model_snapshot"] or "{}")
    quality = json.loads(row["quality"] or "{}")
    return ReviewSession(
        id=row["id"],
        teacher_id=row["teacher_id"],
        teacher_name=row["teacher_name"],
        student_id=row["student_id"],
        student_name=row["student_name"],
        major=row["major"],
        draft_id=row["draft_id"],
        paper_path=row["paper_path"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        model_snapshot=_public_snapshot(snapshot),
        findings=findings,
        final_output_path=row["final_output_path"],
        completed=bool(row["completed"]),
        source_path=row["source_path"],
        output_dir=row["output_dir"],
        quality=quality if isinstance(quality, dict) else {},
    )


def _row_to_feedback(row: sqlite3.Row) -> TeacherFeedback:
    payload = json.loads(row["original_payload"] or "{}")
    return TeacherFeedback(
        id=row["id"],
        session_id=row["session_id"],
        finding_id=row["finding_id"],
        teacher_id=row["teacher_id"],
        student_id=row["student_id"],
        paper_path=row["paper_path"],
        section=row["section"],
        decision=row["decision"],
        original_payload=_strip_secrets(payload if isinstance(payload, dict) else {}),
        edited_text=row["edited_text"],
        created_at=row["created_at"],
        kind=row["kind"],
        category=row["category"],
        problem=row["problem"],
    )


def feedback_as_soft_reference(item: TeacherFeedback, *, layer: str = "student") -> dict:
    """Teacher history is a soft hint. Rejected items must not look like positive rules.

    Both layers (per-student and teacher-global) stay advisory: neither may be
    promoted to a school hard rule.
    """
    decision = item.decision
    if decision == "rejected":
        hint = "老师曾驳回类似意见，不要当作必须再报的正向规则。"
    elif decision == "edited_accepted":
        hint = "老师曾改写后采用类似意见，可参考其最终措辞，但不要自动升格为规则。"
    elif decision == "accepted":
        hint = "老师曾采用过类似意见，仅作软参考，一次采用不能永久当规则。"
    else:
        hint = "仅作软参考。"
    if layer == "global":
        hint += "此条来自老师对所有学生的历史，不代表当前学生的既定问题。"
    return {
        "layer": layer,
        "decision": decision,
        "kind": item.kind or (derive_kind(item.original_payload) if item.original_payload else ""),
        "problem": item.problem,
        "section": item.section,
        "edited_text": item.edited_text if decision == "edited_accepted" else "",
        "hint": hint,
        "positive_rule": False,
    }
