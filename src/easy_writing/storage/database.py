import sqlite3
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path


class StorageError(Exception):
    """可展示的业务错误，不含正文或密钥。"""


class Database:
    VERSION = 10

    def __init__(self, path: Path):
        self.path = Path(path)
        if str(path) == ":memory:":
            raise StorageError("请使用本地数据库文件，以支持跨连接事务和任务恢复。")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != 0:
                raise StorageError("此文件不是小说知识库。")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > self.VERSION:
                raise StorageError("数据库版本高于当前程序支持版本，请升级程序。")
            if 0 < version < self.VERSION:
                backup_path = self.path.with_name(self.path.name + f".pre-v{version+1}.bak")
                if not backup_path.exists():
                    # 不覆盖已有备份；SQLite backup API 保留一致快照。
                    try:
                        with backup_path.open("xb"):
                            pass
                    except FileExistsError:
                        pass
                    else:
                        target = sqlite3.connect(backup_path)
                        try:
                            conn.backup(target)
                        finally:
                            target.close()
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > self.VERSION:
                    raise StorageError("数据库版本高于当前程序支持版本，请升级程序。")
                for target in range(version + 1, self.VERSION + 1):
                    name = "schema.sql" if target == 1 else f"migration_{target:03d}.sql"
                    script = files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8")
                    statement = ""
                    for line in script.splitlines(keepends=True):
                        statement += line
                        if sqlite3.complete_statement(statement):
                            conn.execute(statement)
                            statement = ""
                    if statement.strip():
                        raise StorageError("数据库迁移脚本不完整。")
                    conn.execute(f"PRAGMA user_version={target}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
