"""章节与知识的命令入口；后续界面复用相同存储和入库服务。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from easy_writing.ingestion.chapter_files import read_chapter_file, validate_text
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.llm.deepseek import DeepSeekClient
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import RECORD_TABLES, Repository, digest
from easy_writing.usage import JsonlCallRecorder
from easy_writing import timeline_cli
from easy_writing import retrieval_cli
from easy_writing import checking_cli
from easy_writing import maintenance_cli

COMMANDS = {"project", "chapters", "ingest", "knowledge", "source", "edit", "alias", "merge", "history"} | timeline_cli.COMMANDS | retrieval_cli.COMMANDS | checking_cli.COMMANDS | maintenance_cli.COMMANDS


def register_commands(subparsers):
    timeline_cli.register_commands(subparsers)
    retrieval_cli.register_commands(subparsers)
    checking_cli.register_commands(subparsers)
    maintenance_cli.register_commands(subparsers)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, help="覆盖 DATABASE_PATH")
    common.add_argument("--env-file", type=Path)
    project = subparsers.add_parser("project", help="创建或列出小说项目")
    actions = project.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", parents=[common])
    create.add_argument("name")
    actions.add_parser("list", parents=[common])
    chapters = subparsers.add_parser("chapters", parents=[common], help="查看章节和入库进度")
    chapters.add_argument("--project", required=True)
    ingest = subparsers.add_parser("ingest", parents=[common], help="导入一章并提取有证据的知识")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--project", help="作品 ID（正式导入时必填）")
    ingest.add_argument("--chapter", type=int, help="章节号（正式导入时必填）")
    ingest.add_argument("--title")
    ingest.add_argument("--provider", choices=["fake", "deepseek"])
    ingest.add_argument("--fixture", type=Path, help="离线提取响应，结构为 blocks: {b0001: ...}")
    ingest.add_argument("--retry", action="store_true")
    ingest.add_argument("--max-chars", type=int, default=2400)
    ingest.add_argument("--max-output-tokens", type=int, default=4096)
    ingest.add_argument("--preview", action="store_true", help="预览正文与页码，无需作品或章节号")
    ingest.add_argument("--confirm", action="store_true", help="确认 Word/PDF 解析后的正文")
    ingest.add_argument("--ocr", action="store_true", help="对 PDF 无可用文字的页启用本地 OCR")
    ingest.add_argument("--reviewed-text", type=Path, help="用校订后的 UTF-8 文本入库，并保留可确认的文件出处")
    knowledge = subparsers.add_parser("knowledge", parents=[common], help="分页查看已发布知识")
    knowledge.add_argument("--project", required=True)
    knowledge.add_argument("--kind", choices=RECORD_TABLES, required=True)
    knowledge.add_argument("--include-stale", action="store_true")
    knowledge.add_argument("--limit", type=int, default=100)
    knowledge.add_argument("--offset", type=int, default=0)
    source = subparsers.add_parser("source", parents=[common], help="回查知识的原文、版本和偏移")
    source.add_argument("--kind", choices=RECORD_TABLES, required=True)
    source.add_argument("--id", required=True)
    edit = subparsers.add_parser("edit", parents=[common], help="确认、否决或修正知识")
    edit.add_argument("--kind", choices=RECORD_TABLES, required=True)
    edit.add_argument("--id", required=True)
    edit.add_argument("--field")
    edit.add_argument("--value")
    edit.add_argument("--status", choices=["confirmed", "rejected", "unconfirmed"])
    edit.add_argument("--reason", required=True)
    edit.add_argument("--preview", action="store_true")
    alias = subparsers.add_parser("alias", parents=[common], help="添加作者确认的别名")
    alias.add_argument("--id", required=True)
    alias.add_argument("--name", required=True)
    alias.add_argument("--reason", required=True)
    merge = subparsers.add_parser("merge", parents=[common], help="合并同一身份，保留证据与历史")
    merge.add_argument("--source", required=True)
    merge.add_argument("--target", required=True)
    merge.add_argument("--reason", required=True)
    merge.add_argument("--preview", action="store_true")
    history = subparsers.add_parser("history", parents=[common], help="查看作者修改前后值及原因")
    history.add_argument("--project", required=True)
    history.add_argument("--id")
    history.add_argument("--limit", type=int, default=100)
    history.add_argument("--offset", type=int, default=0)


def read_utf8(path):
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError):
        raise StorageError("无法读取文件，请检查路径和 UTF-8 编码。") from None


def handle_command(args, settings):
    if args.command == "ingest":
        document = read_chapter_file(args.file, ocr=args.ocr, ocr_data=settings.ocr_tessdata_path, ocr_language=settings.ocr_language)
        if args.preview:
            return {"filename": document.filename, "format": document.format, "sha256": document.sha256,
                    "suggested_title": document.title, "text": document.text,
                    "pages": [asdict(page) for page in document.pages], "warnings": list(document.warnings)}
        if not args.project or args.chapter is None:
            raise StorageError("正式导入需要 --project 和 --chapter。")
        if document.format in ("docx", "pdf") and not args.confirm:
            raise StorageError("请先用 --preview 检查 Word/PDF 正文，确认后添加 --confirm。")
        content = read_utf8(args.reviewed_text) if args.reviewed_text else document.text
        validate_text(content)
    repo = Repository(Database(args.db or settings.database_path))
    if args.command in maintenance_cli.COMMANDS:
        return maintenance_cli.handle_command(args, settings, repo)
    if args.command in checking_cli.COMMANDS:
        return checking_cli.handle_command(args, repo)
    if args.command in retrieval_cli.COMMANDS:
        return retrieval_cli.handle_command(args, settings, repo)
    if args.command in timeline_cli.COMMANDS:
        return timeline_cli.handle_command(args, repo)
    if args.command == "project":
        return repo.create_project(args.name) if args.action == "create" else repo.projects()
    if args.command == "chapters":
        return repo.chapters(args.project)
    if args.command == "knowledge":
        return repo.records(args.project, args.kind, include_stale=args.include_stale, limit=args.limit, offset=args.offset)
    if args.command == "source":
        return repo.evidence(args.kind, args.id)
    if args.command == "history":
        return repo.history(args.project, record_id=args.id, limit=args.limit, offset=args.offset)
    if args.command == "edit":
        return repo.impact(args.kind, args.id) if args.preview else repo.edit(
            args.kind, args.id, field=args.field, value=args.value, status=args.status, reason=args.reason
        )
    if args.command == "alias":
        return repo.add_alias(args.id, args.name, args.reason)
    if args.command == "merge":
        return repo.impact("entity", args.source) if args.preview else repo.merge(args.source, args.target, args.reason)
    if not 64 <= args.max_output_tokens <= 131072:
        raise StorageError("输出 token 上限应在 64～131072 之间。")
    if not 200 <= args.max_chars <= 8000:
        raise StorageError("分块长度应在 200～8000 字符之间。")
    for warning in document.warnings:
        print(warning, file=sys.stderr)
    provider = args.provider or settings.provider
    settings = settings.model_copy(update={"max_output_tokens": args.max_output_tokens})
    recorder = JsonlCallRecorder(settings.usage_log, secrets=(settings.api_key.get_secret_value(),))
    if provider == "fake":
        if args.fixture is None:
            raise StorageError("假模型不会分析任意正文；离线入库请提供 --fixture，或选择 --provider deepseek。")
        fixture_text = read_utf8(args.fixture)
        try:
            blocks = json.loads(fixture_text)["blocks"]
            if not isinstance(blocks, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise StorageError("fixture 应为包含 blocks 对象的 JSON 文件。") from None
        client = FixtureLLMClient(recorder, blocks)
        identity = "fake:" + digest(fixture_text)
    else:
        if args.fixture:
            raise StorageError("--fixture 仅用于假模型，不能与 DeepSeek 同时使用。")
        client = DeepSeekClient(settings, recorder)
        identity = ":".join(("deepseek", settings.base_url, settings.extraction_model, settings.extraction_thinking, str(settings.max_output_tokens)))
        print("本次新增或未完成分块将发送至 DeepSeek；重复导入已完成任务会使用本地缓存。", file=sys.stderr)
    def progress(event):
        print(f"入库进度 {event['completed_blocks']}/{event['total_blocks']}（任务 {event['job_id']}）", file=sys.stderr)
    with client:
        return IngestionPipeline(
            repo, client, model_identity=identity, max_chars=args.max_chars,
            lease_seconds=max(900, settings.timeout_seconds * (settings.max_retries + 1) + 120),
        ).ingest(args.project, args.chapter, args.title or document.title, content, retry=args.retry,
                 progress=progress, document=document)
