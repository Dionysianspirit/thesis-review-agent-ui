from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

from thesis_review.errors import ReviewError
from thesis_review.types import (
    CommentRecord,
    ParagraphView,
    ReplaceResult,
    RevisionRecord,
    TableView,
)
from thesis_review.word.engine import ensure_engine


@dataclass
class OpenedDocument:
    committed: bytes
    # Parsed anchor blocks for the current `committed` bytes; owned by
    # WordAdapter and invalidated when a transaction rewrites the document.
    blocks_cache: list[dict] | None = field(default=None, repr=False, compare=False)
    # Live DocxEngine session for the same bytes, so consecutive mutations on
    # one document skip the base64 + unzip + XML parse of a fresh docx_open.
    # Dropped after any failed mutation: the engine may leave the package
    # half-written, and `committed` is the only clean state to fall back to.
    engine_session: object | None = field(default=None, repr=False, compare=False)
    engine_doc_id: str = ""


class WordAdapter:
    def open_bytes(self, data: bytes) -> OpenedDocument:
        if not data:
            raise ReviewError("open_failed", "文档内容为空。")
        return OpenedDocument(committed=data)

    def open_path(self, path: Path | str) -> OpenedDocument:
        target = Path(path)
        if not target.is_file():
            raise ReviewError("open_failed", f"找不到文件：{target}")
        return self.open_bytes(target.read_bytes())

    def export_bytes(self, opened: OpenedDocument) -> bytes:
        return opened.committed

    def save(self, opened: OpenedDocument, path: Path | str) -> None:
        Path(path).write_bytes(opened.committed)

    def list_paragraphs(self, opened: OpenedDocument) -> list[ParagraphView]:
        return [
            ParagraphView(ordinal=item["ordinal"], anchor=item["anchor"], text=item["text"])
            for item in self._blocks(opened)
            if item["kind"] == "paragraph"
        ]

    def list_tables(self, opened: OpenedDocument) -> list[TableView]:
        tables: list[TableView] = []
        previous = ""
        previous_anchor = ""
        for item in self._blocks(opened):
            if item["kind"] == "paragraph":
                previous = item["text"]
                previous_anchor = item["anchor"]
            elif item["kind"] == "table":
                tables.append(
                    TableView(
                        ordinal=item["ordinal"],
                        anchor=item["anchor"],
                        previous_text=previous,
                        previous_anchor=previous_anchor,
                    )
                )
        return tables

    def extract_comments(self, opened: OpenedDocument) -> list[CommentRecord]:
        ensure_engine()
        from docxengine import docx_comment

        session, doc_id = self._session(opened)
        raw = docx_comment(session, doc_id=doc_id, op="list")["comments"]
        by_anchor = {paragraph.anchor: paragraph.text for paragraph in self.list_paragraphs(opened)}
        records: list[CommentRecord] = []
        for item in raw:
            anchor = str(item.get("anchor") or "")
            records.append(
                CommentRecord(
                    comment_id=str(item.get("id") or ""),
                    author=str(item.get("author") or ""),
                    text=str(item.get("text") or ""),
                    anchor=anchor,
                    span=by_anchor.get(anchor, ""),
                    date=str(item.get("date") or ""),
                )
            )
        return records

    def extract_revisions(self, opened: OpenedDocument) -> list[RevisionRecord]:
        ensure_engine()
        from docxengine import docx_revision

        session, doc_id = self._session(opened)
        raw = docx_revision(session, doc_id=doc_id, op="list")["revisions"]
        return [
            RevisionRecord(
                revision_id=str(item.get("id") or ""),
                kind=str(item.get("type") or ""),
                text=str(item.get("text") or ""),
                author=str(item.get("author") or ""),
                date=str(item.get("date") or ""),
            )
            for item in raw
        ]

    def add_comment(self, opened: OpenedDocument, *, anchor: str, text: str, author: str) -> None:
        ensure_engine()
        from docxengine import docx_comment

        def mutate(session, doc_id) -> None:
            docx_comment(session, doc_id=doc_id, op="add", anchor=anchor, text=text, author=author)

        self._transact(opened, mutate)

    def replace_tracked(
        self,
        opened: OpenedDocument,
        *,
        anchor: str,
        old: str,
        new: str,
        author: str,
    ) -> ReplaceResult:
        ensure_engine()
        from docxengine import docx_replace

        def mutate(session, doc_id) -> dict:
            return docx_replace(
                session,
                doc_id=doc_id,
                anchor=anchor,
                old=old,
                new=new,
                track_changes=True,
                author=author,
            )

        raw = self._transact(opened, mutate)
        new_anchor = str(raw.get("new_anchor") or anchor)
        new_text = next(
            (paragraph.text for paragraph in self.list_paragraphs(opened) if paragraph.anchor == new_anchor),
            "",
        )
        return ReplaceResult(
            n_replaced=int(raw.get("n_replaced") or 0),
            new_anchor=new_anchor,
            new_text=new_text,
        )

    def reject_revisions(self, opened: OpenedDocument, *, author: str) -> None:
        ensure_engine()
        from docxengine import docx_revision

        def mutate(session, doc_id) -> None:
            docx_revision(session, doc_id=doc_id, op="reject", filter={"author": author})

        self._transact(opened, mutate)

    def validate(self, opened: OpenedDocument) -> dict:
        ensure_engine()
        from docxengine import docx_validate

        session, doc_id = self._session(opened)
        return docx_validate(session, doc_id=doc_id)

    def _blocks(self, opened: OpenedDocument) -> list[dict]:
        if opened.blocks_cache is not None:
            return opened.blocks_cache
        ensure_engine()
        from docxengine import build_anchor_index

        session, doc_id = self._session(opened)
        document = session.get(doc_id)
        blocks = [
            {
                "kind": entry.kind,
                "ordinal": entry.ordinal,
                "anchor": entry.anchor,
                "text": entry.normalized,
            }
            for entry in build_anchor_index(document.package)
        ]
        opened.blocks_cache = blocks
        return blocks

    def _session(self, opened: OpenedDocument):
        ensure_engine()
        from docxengine import Session, docx_open

        if opened.engine_session is not None:
            # The engine keeps the parsed package alive under doc_id and stays
            # usable across export_bytes calls (verified by probe against the
            # vendored engine); `committed` is only ever replaced by the export
            # of that same package, so the two cannot drift apart.
            return opened.engine_session, opened.engine_doc_id
        session = Session()
        info = docx_open(session, bytes=base64.b64encode(opened.committed).decode("ascii"))
        opened.engine_session = session
        opened.engine_doc_id = str(info["doc_id"])
        return session, opened.engine_doc_id

    def _drop_engine_state(self, opened: OpenedDocument) -> None:
        opened.engine_session = None
        opened.engine_doc_id = ""

    def _transact(self, opened: OpenedDocument, mutate):
        ensure_engine()
        from docxengine import ToolError, export_bytes

        session, doc_id = self._session(opened)
        try:
            extra = mutate(session, doc_id)
            opened.committed = export_bytes(session, doc_id=doc_id)
        except ToolError as exc:
            # A failed mutation can leave the live package half-written while
            # `committed` still holds the last good export; reopen from that
            # on the next call instead of trusting the dirty session.
            self._drop_engine_state(opened)
            raise ReviewError(exc.code, exc.message) from exc
        except Exception:
            self._drop_engine_state(opened)
            raise
        opened.blocks_cache = None
        return extra
