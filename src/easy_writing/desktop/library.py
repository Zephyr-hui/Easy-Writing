"""独立作品目录与可恢复的作品回收站。"""

import json
import re
import sqlite3
import stat
import sys
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from easy_writing.services.chapter_drafting import DRAFT_LEASE_MINUTES
from easy_writing.services.conversation import ConversationService
from easy_writing.services.review_scope import text_value
from easy_writing.storage.chat_database import CHAT_APPLICATION_ID
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid


BOOK_DB = "easy_writing.db"
CHAT_DB = "easy_writing_chat.db"
MANIFEST = "work.json"


def read_only(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def child(parent, name):
    """只接受预期父目录下的普通条目，拒绝符号链接及 Windows 重解析点。"""
    path = parent / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    if (path.parent != parent or (info is not None and (
            stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))
            or path.resolve().parent != parent):
        raise StorageError("作品路径不在指定的安全目录中。")
    return path


class WorkLibrary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        child(self.root, ".recycle")
        lock = child(self.root, ".workspace.lock").open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise StorageError("此作品根目录已被另一桌面窗口打开。") from None
        self.lock = lock

    def close(self):
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def folder(self, work, *, recycled=False):
        if not re.fullmatch(r"[0-9a-f]{32}", work):
            raise StorageError("作品目录标识无效。")
        return child(child(self.root, ".recycle") if recycled else self.root, work)

    def info(self, work, *, recycled=False):
        return self._inspect(self.folder(work, recycled=recycled), work)

    @staticmethod
    def _inspect(folder, work):
        try:
            meta = json.loads(child(folder, MANIFEST).read_text(encoding="utf-8"))
            if meta.get("work_id") != work or not isinstance(meta.get("project_id"), str):
                raise ValueError
            if not child(folder, CHAT_DB).is_file():
                raise ValueError
            with read_only(child(folder, BOOK_DB)) as db:
                rows = db.execute("SELECT id,name,created_at FROM projects").fetchall()
                identity = db.execute("SELECT id FROM knowledge_identity").fetchone()
            if len(rows) != 1 or rows[0]["id"] != meta["project_id"]:
                raise ValueError
            if identity is None or meta.get("library_id") != identity[0]:
                raise ValueError
            with read_only(child(folder, CHAT_DB)) as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != CHAT_APPLICATION_ID or db.execute(
                    "SELECT 1 FROM sessions WHERE library_id<>? OR project_id<>?", (identity[0], meta["project_id"])
                ).fetchone():
                    raise ValueError
                if db.execute("PRAGMA user_version").fetchone()[0] >= 2 and db.execute(
                    "SELECT 1 FROM project_guidance WHERE library_id<>? OR project_id<>?", (identity[0], meta["project_id"])
                ).fetchone():
                    raise ValueError
        except (OSError, ValueError, sqlite3.Error, AttributeError):
            raise StorageError(f"作品目录资料不完整（{work[:8]}），请核对作品文件。") from None
        return dict(rows[0]) | {"work_id": work, "folder": folder, "origin": meta.get("origin")}

    def works(self, *, recycled=False):
        base = child(self.root, ".recycle") if recycled else self.root
        if not base.exists():
            return []
        rows = [self.info(path.name, recycled=recycled) for path in base.iterdir()
                if re.fullmatch(r"[0-9a-f]{32}", path.name)]
        return sorted(rows, key=lambda row: (row["created_at"], row["work_id"]))

    def open(self, work):
        info = self.info(work)
        folder = info["folder"]
        repo = Repository(Database(child(folder, BOOK_DB)))
        return repo, ConversationService(repo, child(folder, CHAT_DB))

    def stage(self, work, populate, *, origin=None):
        destination = self.folder(work)
        if destination.exists() or self.folder(work, recycled=True).exists():
            raise StorageError("作品目录已存在，请从列表或回收站打开。")
        with TemporaryDirectory(prefix=".work-stage-", dir=self.root) as temporary:
            folder = Path(temporary).resolve()
            repo = Repository(Database(folder / BOOK_DB))
            chat = ConversationService(repo, folder / CHAT_DB)
            project = populate(repo, chat)
            (folder / MANIFEST).write_text(json.dumps({"work_id": work, "project_id": project, "library_id": chat.library, "origin": origin},
                                                       ensure_ascii=False), encoding="utf-8")
            info = self._inspect(folder, work)
            child(self.root, folder.name).rename(destination)
        return info | {"folder": destination}

    def create(self, title, *, initialize=None):
        def populate(repo, _chat):
            project = repo.create_project(title)["id"]
            if initialize:
                initialize(repo, project)
            return project
        return self.stage(uid(), populate)

    def rename(self, work, title):
        title = text_value(title, "作品名称", 100)
        info = self.info(work)
        repo, _ = self.open(work)
        with repo.db.transaction() as conn:
            conn.execute("UPDATE projects SET name=? WHERE id=?", (title, info["id"]))

    def delete(self, work):
        info = self.info(work)
        folder = info["folder"]
        assert_idle(folder / BOOK_DB, folder / CHAT_DB, project=info["id"])
        recycle = child(self.root, ".recycle")
        recycle.mkdir(exist_ok=True)
        destination = self.folder(work, recycled=True)
        if destination.exists():
            raise StorageError("作品回收站已有同一标识的目录。")
        try:
            folder.rename(destination)
        except OSError:
            raise StorageError("作品目录暂时无法移动，请关闭占用数据库的程序后重试。") from None

    def restore(self, work):
        info = self.info(work, recycled=True)
        destination = self.folder(work)
        if destination.exists():
            raise StorageError("同一作品已存在于作品根目录。")
        info["folder"].rename(destination)
        return self.info(work)


def assert_idle(knowledge, chat, *, project=None):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with read_only(knowledge) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "ingestion_jobs" in tables and conn.execute(
            "SELECT 1 FROM ingestion_jobs WHERE status='running' AND lease_until>?", (time.time(),)
        ).fetchone():
            raise StorageError("数据库有进行中的入库任务，请等待任务结束。")
        if "work_tasks" in tables and conn.execute(
            "SELECT 1 FROM work_tasks WHERE status='running' AND created_at>=? AND (? IS NULL OR project_id=?)",
            (cutoff, project, project)
        ).fetchone():
            raise StorageError("数据库有进行中的任务，请等待任务结束。")
        draft_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DRAFT_LEASE_MINUTES)).isoformat()
        if "chapter_drafts" in tables and conn.execute(
            "SELECT 1 FROM chapter_drafts WHERE status='running' AND started_at>=? AND (? IS NULL OR project_id=?)",
            (draft_cutoff, project, project)
        ).fetchone():
            raise StorageError("数据库有进行中的章节草稿任务，请等待任务结束。")
    if chat is not None:
        with read_only(chat) as conn:
            if conn.execute("SELECT 1 FROM turns WHERE status='running' AND created_at>=?", (cutoff,)).fetchone():
                raise StorageError("会话库有进行中的问答，请等待任务结束。")
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
            if conn.execute("SELECT 1 FROM summary_attempts WHERE status='running' AND created_at>=?", (cutoff,)).fetchone():
                raise StorageError("会话库有进行中的摘要，请等待任务结束。")
