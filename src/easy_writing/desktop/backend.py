"""桌面端的路径、单章提交和对话编排。"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from easy_writing.config import ConfigurationError, load_settings
from easy_writing.desktop.library import MANIFEST
from easy_writing.ingestion.chapter_files import read_chapter_file, validate_text
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.llm.base import LLMError
from easy_writing.services.conversation import ConversationService
from easy_writing.services.tasks import TaskService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, digest, uid
from easy_writing.ui.clients import client, recorder, workbench_import_identity
from easy_writing.usage import JsonlCallRecorder


def default_data_dir():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "EasyWriting"


def desktop_settings(data_dir, env_file=None):
    config = Path(env_file).resolve() if env_file else data_dir / ".env"
    settings = load_settings(config)
    paths = ("database_path", "chat_database_path", "ocr_tessdata_path", "embedding_cache", "usage_log", "retrieval_log")
    return settings.model_copy(update={key: config.parent / value for key in paths
                                       if (value := getattr(settings, key)) is not None})


def expected_error(error):
    if isinstance(error, (StorageError, ConfigurationError, LLMError)):
        return str(error)
    if isinstance(error, OSError):
        return "无法访问所选文件或运行时，请检查路径和权限。"
    return "操作未完成，请检查输入、数据库或本机配置。"


class DesktopBackend:
    def __init__(self, data_dir, env_file=None):
        self.data_dir = Path(data_dir).resolve()
        self.env_file = Path(env_file).resolve() if env_file else None
        self.settings = desktop_settings(self.data_dir, self.env_file)

    def open(self, knowledge=None, chat=None):
        settings = self.settings
        repo = Repository(Database(Path(knowledge or settings.database_path).resolve()))
        conversation = ConversationService(repo, chat or settings.chat_database_path)
        return repo, conversation

    def preview(self, path, ocr=False):
        return read_chapter_file(Path(path), ocr=ocr, ocr_data=self.settings.ocr_tessdata_path,
                                 ocr_language=self.settings.ocr_language)

    def settings_for(self, repo):
        folder = repo.db.path.parent
        return (self.settings.model_copy(update={"usage_log": folder / "usage.jsonl"})
                if (folder / MANIFEST).is_file() else self.settings)

    def ingest(self, repo, project, chapter, title, text, document, provider, fixture, retry, progress):
        cfg = self.settings_for(repo)
        validate_text(text)
        if not title.strip() or len(title) > 300 or chapter < 1:
            raise StorageError("请填写有效的章节号和标题。")
        if provider == "fake":
            if not fixture:
                raise StorageError("离线导入需要对应的提取响应 JSON，或可使用内置演示样章。")
            try:
                raw = Path(fixture).read_text(encoding="utf-8-sig")
                blocks = json.loads(raw)["blocks"]
                if not isinstance(blocks, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise StorageError("离线响应须为包含 blocks 对象的 JSON。") from None
            identity = workbench_import_identity(cfg, raw)
            adapter = FixtureLLMClient(recorder(cfg), blocks)
        else:
            adapter = client(cfg, provider, output_tokens=4096)
            identity = workbench_import_identity(cfg)
        with adapter:
            return TaskService(repo).perform(
                project, uid(), "ingest", {"chapter": chapter, "content_hash": digest(text), "provider": provider},
                lambda: IngestionPipeline(repo, adapter, model_identity=identity).ingest(
                    project, chapter, title, text, document=document, retry=retry, progress=progress),
            )["result"]

    def ask(self, chat, project, session, request, *, embedding=None, output_tokens=None):
        cfg = self.settings
        managed = (chat.db.path.parent / MANIFEST).is_file()
        # 受管理作品的问答用量随轮次持久化，删除会话时无需跨文件清理日志。
        call_log = SimpleNamespace(record=lambda _: None) if managed else None
        return chat.send(
            project, session, uid(), request, embedding=embedding, model_first=True,
            client_factory=lambda: client(cfg, request.provider, output_tokens=output_tokens or (1536 if request.deep else 1024),
                                          call_recorder=call_log),
            summary_client_factory=lambda: client(cfg, request.provider, output_tokens=384, call_recorder=call_log),
            recorder=None if managed else JsonlCallRecorder(cfg.retrieval_log, secrets=(cfg.api_key.get_secret_value(),)),
        )["result"]

    def semantic_ask(self, repo, chat, project, session, request, interpreter):
        if not interpreter or not Path(interpreter).is_file():
            raise StorageError("请先在设置中选择已安装项目语义依赖的 python.exe。")
        payload = {"knowledge": str(repo.db.path), "chat": str(chat.db.path), "project": project, "session": session,
                   "request": request.model_dump(), "data_dir": str(self.data_dir),
                   "env_file": str(self.env_file) if self.env_file else None}
        try:
            with self.start_semantic(interpreter) as process:
                try:
                    stdout, _ = process.communicate(json.dumps(payload, ensure_ascii=False),
                                                    timeout=600 + self.settings.timeout_seconds * 6)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise
            result = json.loads(stdout)
        except subprocess.TimeoutExpired:
            raise StorageError("本地语义任务超时，请检查模型缓存，再查看已保存的对话轮次。") from None
        except (ValueError, OSError):
            raise StorageError("语义运行时启动失败，请检查所选 Python 和项目语义依赖。") from None
        if "error" in result:
            raise StorageError(result["error"])
        return result["result"]

    def start_semantic(self, interpreter):
        # 冻结程序优先使用自己的 DLL，外部 Python 应继承系统搜索路径。
        reset = sys.platform == "win32" and getattr(sys, "frozen", False)
        if reset:
            from ctypes import windll
            windll.kernel32.SetDllDirectoryW(None)
        try:
            return subprocess.Popen(
                [str(interpreter), "-X", "utf8", "-m", "easy_writing.desktop.semantic"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                cwd=self.data_dir, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        finally:
            if reset:
                windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))
