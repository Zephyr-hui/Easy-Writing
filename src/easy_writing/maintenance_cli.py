"""改稿维护、入库恢复、备份导出与用量统计。"""

import argparse
import json
from pathlib import Path

from easy_writing.services.archive import ArchiveService
from easy_writing.services.recovery import RecoveryService
from easy_writing.services.revisions import RevisionService
from easy_writing.services.statistics import StatisticsService
from easy_writing.storage.database import StorageError
from easy_writing.usage import JsonlCallRecorder

COMMANDS = {"revision", "recovery", "archive", "usage-stats"}


def register_commands(subparsers):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path)
    common.add_argument("--env-file", type=Path)
    scoped = argparse.ArgumentParser(add_help=False, parents=[common])
    scoped.add_argument("--project", required=True)
    revision = subparsers.add_parser("revision", help="改稿依赖、待复核项目与派生资料刷新").add_subparsers(dest="action", required=True)
    status = revision.add_parser("status", parents=[scoped])
    status.add_argument("--limit", type=int, default=50)
    status.add_argument("--offset", type=int, default=0)
    depend = revision.add_parser("depend", parents=[scoped])
    for name in ("source-kind", "target-kind"):
        depend.add_argument("--" + name, choices=["entity", "fact", "event", "thread", "summary", "link"], required=True)
    for name in ("source", "target", "reason"):
        depend.add_argument("--" + name, required=True)
    resolve = revision.add_parser("resolve", parents=[scoped])
    resolve.add_argument("--review", required=True)
    resolve.add_argument("--reason", required=True)
    refresh = revision.add_parser("refresh", parents=[scoped])
    refresh.add_argument("--embedding", choices=["none", "fake", "bge"], default="none")
    recovery = subparsers.add_parser("recovery", help="查看与显式继续入库任务").add_subparsers(dest="action", required=True)
    recovery.add_parser("jobs", parents=[scoped])
    resume = recovery.add_parser("resume", parents=[scoped])
    resume.add_argument("--job", required=True)
    resume.add_argument("--provider", choices=["fake", "deepseek"], default="fake")
    resume.add_argument("--fixture", type=Path)
    archive = subparsers.add_parser("archive", help="完整数据库备份、恢复到新文件与当前作品知识导出").add_subparsers(dest="action", required=True)
    backup = archive.add_parser("backup", parents=[common])
    backup.add_argument("--output", type=Path, required=True)
    restore = archive.add_parser("restore", parents=[common])
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    export = archive.add_parser("export", parents=[scoped])
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--no-sources", action="store_true")
    stats = subparsers.add_parser("usage-stats", parents=[common], help="逐次尝试汇总用量，未知费用不算零")
    stats.add_argument("--project")
    stats.add_argument("--log", type=Path, action="append")


def embedding_provider(name, settings):
    if name == "none":
        return None
    from easy_writing.retrieval.embeddings import BGEEmbeddingProvider, FakeEmbeddingProvider
    return FakeEmbeddingProvider() if name == "fake" else BGEEmbeddingProvider(settings.embedding_model, cache_folder=settings.embedding_cache, revision=settings.embedding_revision)


def resume_client(settings, provider, fixture_bytes=None):
    recorder = JsonlCallRecorder(settings.usage_log, secrets=(settings.api_key.get_secret_value(),))
    if provider == "deepseek":
        from easy_writing.llm.deepseek import DeepSeekClient
        return DeepSeekClient(settings.model_copy(update={"max_retries": 0}), recorder)
    if not fixture_bytes:
        raise StorageError("离线恢复需要提供 blocks 格式的 --fixture。")
    try:
        blocks = json.loads(fixture_bytes.decode("utf-8-sig"))["blocks"]
        if not isinstance(blocks, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise StorageError("离线恢复响应须包含 blocks 对象。") from None
    from easy_writing.ingestion.extractor import FixtureLLMClient
    return FixtureLLMClient(recorder, blocks)


def handle_command(args, settings, repo):
    if args.command == "revision":
        service = RevisionService(repo)
        if args.action == "status":
            return service.status(args.project, limit=args.limit, offset=args.offset)
        if args.action == "depend":
            return service.depend(args.project, args.source_kind, args.source, args.target_kind, args.target, reason=args.reason)
        if args.action == "resolve":
            return service.resolve(args.project, args.review, reason=args.reason)
        return service.refresh(args.project, provider=embedding_provider(args.embedding, settings))
    if args.command == "recovery":
        service = RecoveryService(repo)
        if args.action == "jobs":
            return service.jobs(args.project)
        recovered_settings = settings.model_copy(update={"max_output_tokens": service.output_budget(args.project, args.job, settings.max_output_tokens)})
        client = resume_client(recovered_settings, args.provider, args.fixture.read_bytes() if args.fixture else None)
        try:
            return service.resume(args.project, args.job, client)
        finally:
            client.close()
    if args.command == "archive":
        service = ArchiveService(repo)
        if args.action == "backup":
            return service.backup(args.output)
        if args.action == "restore":
            return service.restore(args.backup, args.output)
        return service.export(args.project, args.output, include_sources=not args.no_sources)
    return StatisticsService(repo).summarize(args.log or [settings.usage_log], project=args.project)
