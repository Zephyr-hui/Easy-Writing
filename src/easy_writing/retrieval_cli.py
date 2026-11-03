"""语义索引、混合检索、受限研究问答和指定版本原文入口。"""

import argparse
import sys
from pathlib import Path

COMMANDS = {"index", "search", "research", "read-original"}


def register_commands(subparsers):
    for name in sorted(COMMANDS):
        parser = subparsers.add_parser(name, help={"index": "增量建立知识向量索引", "search": "混合检索与资料包（不调用 LLM）",
                                                  "research": "LangGraph + 只读 Tools 问答", "read-original": "按任务范围回查指定版本原文"}[name])
        parser.add_argument("--db", type=Path)
        parser.add_argument("--env-file", type=Path)
        parser.add_argument("--project", required=True)
        if name != "index":
            parser.add_argument("--chapter", type=int)
            parser.add_argument("--actor")
            parser.add_argument("--at-event")
        if name in ("search", "research"):
            parser.add_argument("--question", required=True)
            parser.add_argument("--subject")
            parser.add_argument("--predicate")
            parser.add_argument("--confirmed-only", action="store_true")
            parser.add_argument("--limit", type=int, default=8)
            parser.add_argument("--context-budget", type=int, default=6000)
            parser.add_argument("--source-mode", choices=["auto", "always", "never"], default="auto")
            parser.add_argument("--max-source-reads", type=int, default=2)
        if name in ("index", "search", "research"):
            parser.add_argument("--embedding", choices=["bge", "fake", "none"], default=None)
        if name == "research":
            parser.add_argument("--provider", choices=["fake", "deepseek"])
            parser.add_argument("--max-llm-calls", type=int, default=3)
            parser.add_argument("--max-tool-calls", type=int, default=3)
            parser.add_argument("--input-budget", type=int, default=12000)
            parser.add_argument("--max-output-tokens", type=int, default=512)
        if name == "read-original":
            parser.add_argument("--key", action="append", required=True)
            parser.add_argument("--version-id")
            parser.add_argument("--max-chars", type=int, default=1600)


def handle_command(args, settings, repo):
    from easy_writing.retrieval.corpus import CorpusBuilder
    from easy_writing.storage.database import StorageError
    from easy_writing.usage import JsonlCallRecorder
    builder = CorpusBuilder(repo)
    scope = {"through_chapter": getattr(args, "chapter", None), "actor": getattr(args, "actor", None), "at_event": getattr(args, "at_event", None)}
    if args.command == "read-original":
        from easy_writing.retrieval.sources import SourceReader
        if not 1 <= args.max_chars <= 4000:
            raise StorageError("原文字符预算应在 1～4000 之间。")
        corpus = builder.build(args.project, **scope)
        return SourceReader(repo, corpus, max_reads=1, max_chars=args.max_chars).read(args.key, version_id=args.version_id)
    from easy_writing.retrieval.embeddings import BGEEmbeddingProvider, FakeEmbeddingProvider
    backend = args.embedding or settings.embedding_backend
    provider = None
    if backend == "fake":
        provider = FakeEmbeddingProvider()
    elif backend == "bge":
        provider = BGEEmbeddingProvider(settings.embedding_model, cache_folder=settings.embedding_cache, revision=settings.embedding_revision)
    if args.command == "index":
        if provider is None:
            raise StorageError("建立向量索引需要 bge 或 fake 嵌入后端。")
        from easy_writing.retrieval.index import VectorIndex
        corpus = builder.build(args.project)
        result = VectorIndex(repo, provider).ensure(corpus)
        builder.check(corpus)
        return {**result, "records": len(corpus.records), "snapshot_id": corpus.snapshot_id}
    from easy_writing.retrieval.workflow import DemoResearchClient, ResearchWorkflow
    client = None
    recorder = JsonlCallRecorder(settings.retrieval_log, secrets=(settings.api_key.get_secret_value(),))
    options = dict(context_budget=args.context_budget, max_source_reads=args.max_source_reads,
                   source_mode=args.source_mode, limit=args.limit)
    if args.command == "research":
        if not 64 <= args.max_output_tokens <= 8192:
            raise StorageError("研究问答输出上限应在 64～8192 token 之间。")
        llm_recorder = JsonlCallRecorder(settings.usage_log, secrets=(settings.api_key.get_secret_value(),))
        if (args.provider or settings.provider) == "deepseek":
            from easy_writing.llm.deepseek import DeepSeekClient
            client = DeepSeekClient(settings.model_copy(update={"max_output_tokens": args.max_output_tokens}), llm_recorder)
            print("本次受限资料包将发送至 DeepSeek；工具调用和模型轮次受预算限制。", file=sys.stderr)
        else:
            client = DemoResearchClient(llm_recorder)
        options.update(input_budget=args.input_budget, max_llm_calls=args.max_llm_calls, max_tool_calls=args.max_tool_calls)
    try:
        return ResearchWorkflow(repo, provider, client=client, recorder=recorder, **options).run(
            args.project, args.question, **scope, subject=args.subject, predicate=args.predicate, confirmed_only=args.confirmed_only)
    finally:
        if client:
            client.close()
