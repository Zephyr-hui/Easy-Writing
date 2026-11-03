"""本地小说创作助手命令入口；默认不会产生付费调用。"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from easy_writing.config import ConfigurationError, Settings, load_settings
from easy_writing.llm.base import LLMError
from easy_writing.llm.deepseek import DeepSeekClient
from easy_writing.llm.fake import FakeLLMClient
from easy_writing.schemas import DEMO_PASSAGE, DemoExtraction
from easy_writing.usage import JsonlCallRecorder
from easy_writing.chapter_cli import COMMANDS, handle_command, register_commands
from easy_writing.storage.database import StorageError


def create_client(settings: Settings, provider: str):
    recorder = JsonlCallRecorder(
        settings.usage_log, secrets=(settings.api_key.get_secret_value(),)
    )
    return FakeLLMClient(recorder) if provider == "fake" else DeepSeekClient(settings, recorder)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Easy-Writing 小说创作助手")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("doctor", "检查配置，不发送网络请求"),
        ("demo", "验收文本和 JSON 接口，默认使用离线假模型"),
        ("ask", "发送一个文本问题"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--provider", choices=["fake", "deepseek"], default=None)
        command.add_argument("--env-file", type=Path, default=None, help="指定 .env 路径")
        if name == "demo":
            command.add_argument("--mode", choices=["both", "text", "json"], default="both")
        if name == "ask":
            command.add_argument("prompt", help="要发送的问题")
    register_commands(subparsers)
    return parser


def _display(label: str, result):
    content = result.content
    if hasattr(content, "model_dump"):
        content = content.model_dump()
    print(json.dumps({
        "task": label, "content": content, "provider": result.provider,
        "model": result.model, "attempts": result.attempts, "call_id": result.call_id,
        "usage": result.usage.model_dump(),
    }, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    # Windows 重定向/管道可能使用本地代码页；统一为与文件日志一致的 UTF-8。
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.env_file)
        if args.env_file is not None and not args.env_file.is_file():
            raise ConfigurationError("指定的 --env-file 文件不存在。")
        if args.command in COMMANDS:
            print(json.dumps(handle_command(args, settings), ensure_ascii=False, indent=2))
            return 0
        provider = args.provider or settings.provider
        if provider == "deepseek":
            settings.require_deepseek_key()
        if args.command == "doctor":
            print(json.dumps({
                "provider": provider,
                "api_key_configured": bool(settings.api_key.get_secret_value()),
                "extraction_model": settings.extraction_model,
                "analysis_model": settings.analysis_model,
                "max_attempts": settings.max_retries + 1,
                "usage_log": str(settings.usage_log),
                "network_request_sent": False,
            }, ensure_ascii=False, indent=2))
            return 0
        print(
            "使用离线假模型：返回固定演示数据，不发送网络请求。" if provider == "fake"
            else "使用 DeepSeek：本次输入将发送至配置的接口，并可能产生费用。",
            file=sys.stderr,
        )
        with create_client(settings, provider) as client:
            if args.command == "ask":
                _display("text", client.generate_text(args.prompt))
            else:
                if args.mode in ("both", "text"):
                    _display("text", client.generate_text("请用一句话说明你可以辅助小说创作。"))
                if args.mode in ("both", "json"):
                    result = client.extract_json(
                        "请提取以下文本中的人物和事件，evidence 必须逐字引用原文：\n" + DEMO_PASSAGE,
                        DemoExtraction,
                    )
                    if any(event.evidence not in DEMO_PASSAGE for event in result.content.events):
                        raise LLMError("演示提取的证据不匹配原文。", code="evidence_mismatch")
                    _display("json", result)
        return 0
    except (ConfigurationError, LLMError, StorageError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError):
        print("错误：数据库或本地文件操作失败，请检查路径、权限与文件状态。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中止；已发送的请求仍可能计费。", file=sys.stderr)
        return 130
