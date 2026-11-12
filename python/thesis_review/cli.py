from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from thesis_review.demo import run_teacher_demo
from thesis_review.history.store import HistoryStore
from thesis_review.paths import app_home
from thesis_review.service import ThesisReviewService
from thesis_review.settings import load_settings
from thesis_review.word.adapter import WordAdapter


def build_service(home: Path) -> ThesisReviewService:
    home.mkdir(parents=True, exist_ok=True)
    return ThesisReviewService(
        store=HistoryStore(home / "thesis-review.sqlite"),
        adapter=WordAdapter(),
        home=home,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thesis-review", description="毕业论文审改助手（开发用命令行）")
    parser.add_argument("--home", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="导入历史稿")
    ingest.add_argument("--teacher", required=True)
    ingest.add_argument("--student", required=True)
    ingest.add_argument("--major", default="人工智能")
    ingest.add_argument("--draft", required=True)
    ingest.add_argument("docx", type=Path)

    confirm = sub.add_parser("confirm", help="确认一条历史问题")
    confirm.add_argument("--teacher", required=True)
    confirm.add_argument("--student", required=True)
    confirm.add_argument("--issue", required=True)

    review = sub.add_parser("review", help="审查新稿")
    review.add_argument("--teacher", required=True)
    review.add_argument("--student", required=True)
    review.add_argument("--draft", default="new")
    review.add_argument("--offline", action="store_true")
    review.add_argument("--out", type=Path, required=True)
    review.add_argument("docx", type=Path, nargs="?")

    demo = sub.add_parser("demo", help="用内置模拟稿跑通老师确认门：初审候选 → 老师决定 → 正式 Word 批注")
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument(
        "--faux",
        action="store_true",
        help="经 Pi Agent 与 worker TCP 做第一轮初审（打包冒烟用，不回退离线规则）",
    )

    export = sub.add_parser("export", help="将老师已确认意见写入正式 Word")
    export.add_argument("--session", required=True, help="Review Session id")
    export.add_argument("--out", type=Path, default=None, help="正式稿路径或目录")
    export.add_argument("--allow-pending", action="store_true", help="忽略未处理项，只写入已确认意见")

    evaluate = sub.add_parser("eval", help="用本机密钥跑真实模型金标（不进 CI）")
    evaluate.add_argument("--out", type=Path, required=True)
    evaluate.add_argument("--manifest", type=Path, default=None, help="可选：本机授权真稿清单，不提交")

    aggregate = sub.add_parser(
        "eval-aggregate",
        help="汇总多次审稿的评测数据（本机、只读），写出 JSON 与 CSV",
    )
    aggregate.add_argument("--out", type=Path, required=True, help="输出目录")
    aggregate.add_argument("--teacher", default="", help="只统计该老师的审稿；缺省为全部")

    args = parser.parse_args(argv)
    home = args.home or app_home()
    service = build_service(home)

    if args.command == "ingest":
        records = service.ingest_history(
            teacher_id=args.teacher,
            student_id=args.student,
            major=args.major,
            draft_id=args.draft,
            data=args.docx.read_bytes(),
        )
        print(json.dumps([record.id for record in records], ensure_ascii=False))
        return 0
    if args.command == "confirm":
        record = service.confirm_issue(
            teacher_id=args.teacher, student_id=args.student, issue_id=args.issue
        )
        print(record.status)
        return 0
    if args.command == "review":
        if args.docx is None:
            raise SystemExit("review 需要 docx 路径")
        result = service.review(
            teacher_id=args.teacher,
            student_id=args.student,
            draft_id=args.draft,
            data=args.docx.read_bytes(),
            output_dir=args.out,
            use_model=not args.offline,
            settings=load_settings(home),
        )
        print(result.reviewed_path)
        print(result.findings_path)
        return 0
    if args.command == "demo":
        payload = run_teacher_demo(service, args.out, faux=bool(args.faux))
        print(payload["reviewed_path"])
        print(payload["findings_path"])
        print(payload["gate_path"])
        if not payload.get("ok"):
            print(payload.get("error") or "teacher-gate demo failed", file=sys.stderr)
            return 1
        return 0
    if args.command == "export":
        dest = args.out
        if dest is not None and dest.suffix.lower() != ".docx":
            dest.mkdir(parents=True, exist_ok=True)
            session = service.sessions.get(args.session)
            dest = dest / f"{session.draft_id}-reviewed.docx"
        result = service.export_final(
            session_id=args.session,
            allow_pending=args.allow_pending,
            dest=dest,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "eval-aggregate":
        from thesis_review.evalaggregate import aggregate_from_store, summary_lines, write_aggregate

        payload = aggregate_from_store(service.sessions, teacher_id=args.teacher)
        written = write_aggregate(payload, args.out)
        for line in summary_lines(payload):
            print(line)
        print("aggregate: " + written["json"])
        return 0
    if args.command == "eval":
        from thesis_review.evalrun import run_eval_command

        return run_eval_command(
            settings_home=home,
            out=args.out,
            manifest=args.manifest,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
