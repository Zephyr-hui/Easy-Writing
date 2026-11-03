"""独立会话文件与阶段八数据迁移。"""

import json
import sqlite3
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from easy_writing.services.archive import ArchiveService, SQLiteArchive
from easy_writing.services.chat_memory import encode, reference_only, save_turn_memory
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import uid


CHAT_APPLICATION_ID = 0x45574348


class ChatDatabase(Database):
    VERSION = 2

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if app_id == CHAT_APPLICATION_ID:
                valid = 1 <= version <= self.VERSION
            else:
                valid = not app_id and not version and not conn.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
            if not valid:
                raise StorageError("此文件不是受支持的会话库。")
            if version == self.VERSION:
                conn.commit()
                return
            for target in range(version + 1, self.VERSION + 1):
                name = "chat_schema.sql" if target == 1 else f"chat_migration_{target:03d}.sql"
                schema = files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8")
                for statement in schema.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            conn.execute(f"PRAGMA application_id={CHAT_APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={self.VERSION}")
            conn.commit()

    @classmethod
    def verify(cls, path):
        try:
            with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if (conn.execute("PRAGMA application_id").fetchone()[0] != CHAT_APPLICATION_ID or not 1 <= version <= cls.VERSION
                        or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchone()):
                    raise StorageError("会话备份的类型、版本或完整性校验未通过。")
                conn.execute("SELECT id FROM sessions LIMIT 1")
                if version >= 2:
                    conn.execute("SELECT project_id,goal,constraints_text FROM project_guidance LIMIT 1")
                return {"schema_version": version, "integrity_check": "ok", "foreign_key_check": "ok"}
        except sqlite3.Error:
            raise StorageError("文件不是可恢复的会话数据库。") from None

    def archive(self):
        return SQLiteArchive(self, self.verify, "easy-writing-chat-backup-v1")


def migrate_legacy(repo, chat, library):
    # 先持久化并逐条核验目标。若中途退出，原库回滚；重启可验证并复用已复制的记录。
    with repo.db.connect() as conn:
        if not conn.execute("SELECT 1 FROM chat_sessions LIMIT 1").fetchone():
            return
    with repo.db.transaction() as source:
        sessions = source.execute("SELECT * FROM chat_sessions ORDER BY rowid").fetchall()
        if not sessions:
            return
        rows = source.execute("""SELECT t.session_id,t.request_json,w.*,s.project_id AS session_project
            FROM chat_turns t JOIN chat_sessions s ON s.id=t.session_id JOIN work_tasks w ON w.id=t.task_id
            ORDER BY t.seq""").fetchall()
        if any(r["project_id"] != r["session_project"] or r["kind"] != "research" for r in rows):
            raise StorageError("旧会话的项目或任务关联校验失败。")
        backup = repo.db.path.with_name(repo.db.path.name + f".pre-chat-split-{uid()[:8]}.bak")
        ArchiveService(repo).backup(backup)
        with chat.transaction() as target:
            for session in sessions:
                expected = (session["project_id"], session["title"], session["created_at"])
                keys = (library, session["id"])
                target.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)", (*keys, *expected))
                actual = target.execute("SELECT project_id,title,created_at FROM sessions WHERE library_id=? AND id=?", keys).fetchone()
                if tuple(actual or ()) != expected:
                    raise StorageError("会话迁移遇到冲突，旧记录已保留。")
            for row in rows:
                result = reference_only(json.loads(row["result_json"])) if row["result_json"] else None
                expected = (row["session_id"], row["request_json"], row["request_hash"], row["status"],
                            encode(result) if result is not None else None, row["error_code"], row["created_at"], row["finished_at"])
                keys = (library, row["id"])
                target.execute("""INSERT OR IGNORE INTO turns(library_id,id,session_id,request_json,request_hash,status,
                    result_json,error_code,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)""", (*keys, *expected))
                actual = target.execute("""SELECT seq,session_id,request_json,request_hash,status,result_json,error_code,created_at,finished_at
                    FROM turns WHERE library_id=? AND id=?""", keys).fetchone()
                if not actual or tuple(actual)[1:] != expected:
                    raise StorageError("对话轮次迁移校验失败，旧记录已保留。")
                if row["status"] == "succeeded" and result:
                    save_turn_memory(target, actual["seq"], library, row["session_id"], row["id"], json.loads(row["request_json"]), result)
        source.execute("DELETE FROM chat_turns")
        source.execute("DELETE FROM chat_sessions")
        source.executemany("DELETE FROM work_tasks WHERE id=?", [(row["id"],) for row in rows])
