"""一致快照备份、恢复到新文件、按作品导出当前知识。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from easy_writing.services.review_scope import ReviewScope
from easy_writing.services.revisions import RevisionService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import RECORD_TABLES, now


def file_hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_database(path):
    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if not 1 <= version <= Database.VERSION or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageError("备份的版本、完整性或外键校验未通过。")
            conn.execute("SELECT id FROM projects LIMIT 1").fetchall()
            return {"schema_version": version, "integrity_check": "ok", "foreign_key_check": "ok"}
    except sqlite3.Error:
        raise StorageError("文件不是可恢复的小说数据库。") from None


class SQLiteArchive:
    def __init__(self, db, verify, format_name):
        self.db, self.verify, self.format_name = db, verify, format_name

    def backup(self, destination):
        target = Path(destination).resolve()
        if target == self.db.path.resolve() or target.exists() or Path(str(target) + ".json").exists():
            raise StorageError("备份必须使用未占用的新文件，不能覆盖当前库或已有文件。")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        try:
            with self.db.connect() as source, closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
            metadata = {"format": self.format_name, "created_at": now(), "sha256": file_hash(target),
                "source": str(self.db.path.resolve()), **self.verify(target), "includes_all_projects": True}
            with Path(str(target) + ".json").open("x", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2)
            return {"backup_path": str(target), "metadata": metadata}
        except BaseException:
            # 仅移除本函数排他创建的未完成文件，不触及已有文件。
            target.unlink(missing_ok=True)
            raise

    def restore(self, backup, destination):
        source, target = Path(backup).resolve(), Path(destination).resolve()
        if not source.is_file() or target.exists() or target in (source, self.db.path.resolve()):
            raise StorageError("恢复源必须存在，目标须是新文件；不能覆盖正在使用的数据库。")
        verified = self.verify(source)
        manifest = Path(str(source) + ".json")
        if manifest.is_file():
            try:
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                if metadata.get("sha256") != file_hash(source):
                    raise StorageError("备份内容与校验清单不符，拒绝恢复。")
            except (ValueError, AttributeError):
                raise StorageError("备份校验清单无效。") from None
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as original, closing(sqlite3.connect(target)) as restored:
                original.backup(restored)
            self.verify(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {"restored_path": str(target), "verified": verified, "active_database_replaced": False,
                "note": "恢复到独立新文件；请手动切换数据库，原工作库继续保留。"}


class ArchiveService(SQLiteArchive):
    def __init__(self, repo):
        super().__init__(repo.db, verify_database, "easy-writing-sqlite-backup-v1")
        self.repo = repo

    def export(self, project, destination, *, include_sources=True):
        target = Path(destination).resolve()
        if target.exists():
            raise StorageError("导出请使用新文件名，避免覆盖已有数据。")
        with self.repo.db.connect() as conn:
            scope = ReviewScope(self.repo, conn, project)
            output = {"format": "easy-writing-knowledge-v1", "project": dict(self.repo.require(conn, "projects", project)),
                "created_at": now(), "snapshot_id": scope.snap["snapshot_id"], "coverage": scope.knowledge._coverage(conn, scope.snap),
                "current_only": True, "records": {}, "chapters": [], "dependencies": [], "plans_are_facts": False}
            for chapter in self.repo.visible(conn, project):
                item = {k: chapter[k] for k in ("id", "chapter_no", "title", "knowledge_version_id", "published_generation_id")}
                if include_sources:
                    item["content"] = conn.execute("SELECT content FROM chapter_versions WHERE id=?", (chapter["knowledge_version_id"],)).fetchone()[0]
                output["chapters"].append(item)
            keys = set()
            for kind, table in RECORD_TABLES.items():
                records, offset = [], 0
                while True:
                    batch = self.repo.records(project, kind, limit=500, offset=offset)
                    records.extend(batch)
                    if len(batch) < 500:
                        break
                    offset += 500
                records = [r for r in records if not r.get("requires_review")]
                for record in records:
                    keys.add((kind, record["id"]))
                    record["evidence"] = scope.refs(kind, [record["id"]]) if kind != "entity" else []
                output["records"][kind] = records
            for edge in conn.execute("SELECT * FROM record_dependencies WHERE project_id=?", (project,)):
                if (edge["source_type"], edge["source_id"]) in keys and (edge["target_type"], edge["target_id"]) in keys:
                    output["dependencies"].append(dict(edge))
            output["author_history"] = [dict(r) for r in conn.execute("SELECT * FROM edit_history WHERE project_id=? ORDER BY created_at,id", (project,))]
            # 历史只导出决策元数据，避免旧事实全文被误当成当前知识。
            for entry in output["author_history"]:
                entry.pop("before_json", None)
                entry.pop("after_json", None)
            # Repo 分页用独立连接，保存前检查仍是同一快照。
            with self.repo.db.connect() as current:
                if ReviewScope(self.repo, current, project).snap["snapshot_id"] != scope.snap["snapshot_id"]:
                    raise StorageError("导出期间知识已变化，请重新导出。")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
        return {"export_path": str(target), "snapshot_id": output["snapshot_id"],
            "record_counts": {k: len(v) for k,v in output["records"].items()}, "includes_sources": include_sources,
            "note": "这是当前作品的知识交换文件；完整恢复请使用 SQLite 备份。"}
