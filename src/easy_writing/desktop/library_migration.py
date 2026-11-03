"""从只读旧库快照按作品复制，并核验两库的引用。"""

import json
import shutil
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from easy_writing.desktop.library import BOOK_DB, CHAT_DB, assert_idle, child, read_only
from easy_writing.services.archive import file_hash, verify_database
from easy_writing.services.conversation import ConversationService
from easy_writing.storage.chat_database import ChatDatabase
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid


PROJECT_TABLES = (
    "chapters", "entities", "edit_history", "plot_threads", "graph_revisions", "vector_segments",
    "world_constraints", "knowledge_uses", "check_runs", "finding_decisions", "finding_decision_history",
    "work_tasks", "chapter_plans", "chapter_drafts", "revision_changes", "record_dependencies", "revision_reviews",
    "record_successions", "derived_views", "chat_sessions",
)

# 以本次已复制的父表定位子记录。FK 延迟到事务结束检查，涵盖章节与版本间的循环引用。
PARENT_TABLES = {
    "chapter_versions": ("chapter_id", "chapters", "id"),
    "ingestion_jobs": ("version_id", "chapter_versions", "id"),
    "source_chunks": ("version_id", "chapter_versions", "id"),
    "chapter_file_sources": ("version_id", "chapter_versions", "id"),
    "extraction_blocks": ("job_id", "ingestion_jobs", "id"),
    "ingestion_specs": ("job_id", "ingestion_jobs", "id"),
    "ingestion_attempts": ("job_id", "ingestion_jobs", "id"),
    "entity_aliases": ("entity_id", "entities", "id"),
    "entity_mentions": ("entity_id", "entities", "id"),
    "events": ("generation_id", "ingestion_jobs", "id"),
    "event_entities": ("event_id", "events", "id"),
    "event_mentions": ("event_id", "events", "id"),
    "event_identity": ("event_id", "events", "id"),
    "event_times": ("event_id", "events", "id"),
    "event_thread_roles": ("plot_thread_id", "plot_threads", "id"),
    "facts": ("generation_id", "ingestion_jobs", "id"),
    "fact_modes": ("fact_id", "facts", "id"),
    "fact_awareness": ("fact_id", "facts", "id"),
    "event_links": ("generation_id", "ingestion_jobs", "id"),
    "link_visibility": ("link_id", "event_links", "id"),
    "threads": ("generation_id", "ingestion_jobs", "id"),
    "summaries": ("generation_id", "ingestion_jobs", "id"),
    "evidence_links": ("source_chunk_id", "source_chunks", "id"),
    "thread_transitions": ("thread_id", "threads", "id"),
    "memory_items": ("generation_id", "ingestion_jobs", "id"),
    "dependencies": ("generation_id", "ingestion_jobs", "id"),
    "check_findings": ("run_id", "check_runs", "id"),
    "plan_decisions": ("plan_id", "chapter_plans", "id"),
    "chat_turns": ("session_id", "chat_sessions", "id"),
    "embedding_models": ("fingerprint", "vector_segments", "model_fingerprint"),
}


def table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM legacy.sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def copy_rows(conn, table, where, params=(), *, library=None):
    count = conn.execute(f"SELECT COUNT(*) FROM legacy.{table} WHERE {where}", params).fetchone()[0]
    if library is None:
        columns, values, prefix = "", "*", ()
    else:
        fields = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
        columns = "(" + ",".join(fields) + ")"
        values = ",".join("?" if name == "library_id" else name for name in fields)
        prefix = (library,)
    conn.execute(f"INSERT INTO main.{table} {columns} SELECT {values} FROM legacy.{table} WHERE {where}", (*prefix, *params))
    if conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0] != count:
        raise StorageError(f"复制后数量核对失败（{table}）。")


def copy_knowledge(source, target, project):
    with target.db.transaction() as conn:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source.db.path),))
        conn.execute("PRAGMA defer_foreign_keys=ON")
        expected = {"projects", "knowledge_identity", *PROJECT_TABLES, *PARENT_TABLES}
        if table_names(conn) != expected:
            raise StorageError("知识库表结构超出当前迁移程序的范围。")
        copy_rows(conn, "projects", "id=?", (project,))
        for table in PROJECT_TABLES:
            copy_rows(conn, table, "project_id=?", (project,))
        for table, (key, parent, reference) in PARENT_TABLES.items():
            copy_rows(conn, table, f"{key} IN (SELECT {reference} FROM main.{parent})")
        if conn.execute("PRAGMA main.foreign_key_check").fetchone():
            raise StorageError("知识库存在跨作品引用或无效外键。")


def copy_chat(source, target, project):
    with target.db.transaction() as conn:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source.db.path),))
        expected = {"sessions", "turns", "turn_memory", "rolling_memory", "summary_attempts", "project_guidance"}
        if table_names(conn) != expected:
            raise StorageError("会话库表结构超出当前迁移程序的范围。")
        copy_rows(conn, "sessions", "library_id=? AND project_id=?", (source.library, project), library=target.library)
        copy_rows(conn, "project_guidance", "library_id=? AND project_id=?", (source.library, project), library=target.library)
        for table in ("turns", "rolling_memory"):
            copy_rows(conn, table, "library_id=? AND session_id IN (SELECT id FROM main.sessions)", (source.library,),
                      library=target.library)
        for table in ("turn_memory", "summary_attempts"):
            copy_rows(conn, table, "turn_seq IN (SELECT seq FROM main.turns)")
        if conn.execute("PRAGMA main.foreign_key_check").fetchone():
            raise StorageError("会话库存在无效外键。")


def backup_pair(library, knowledge, chat):
    directory = child(library.root, ".migration-backups")
    directory.mkdir(exist_ok=True)
    backup = child(directory, uid())
    backup.mkdir()
    paths = [(knowledge, backup / BOOK_DB, verify_database)]
    if chat:
        paths.append((chat, backup / CHAT_DB, ChatDatabase.verify))
    with ExitStack() as stack:
        sources = [(stack.enter_context(read_only(source)), target, check) for source, target, check in paths]
        versions = [conn.execute("PRAGMA data_version").fetchone()[0] for conn, _, _ in sources]
        for conn, _, _ in sources:
            conn.execute("BEGIN")
            conn.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
        for conn, target, _ in sources:
            with closing(sqlite3.connect(target)) as out:
                conn.backup(out)
        for conn, _, _ in sources:
            conn.commit()
        if versions != [conn.execute("PRAGMA data_version").fetchone()[0] for conn, _, _ in sources]:
            raise StorageError("备份期间旧库发生写入，请关闭旧版程序后重试。")
    for _, target, check in paths:
        check(target)
    (backup / "checksums.json").write_text(
        json.dumps({target.name: file_hash(target) for _, target, _ in paths}, indent=2), encoding="utf-8")
    return backup


def migrate(library, knowledge, chat=None):
    knowledge = Path(knowledge).resolve()
    chat = Path(chat).resolve() if chat else None
    if not knowledge.is_file() or (chat and (not chat.is_file() or knowledge == chat)):
        raise StorageError("请选择有效的旧知识库与独立会话库文件。")
    for path in (knowledge, chat):
        if path is not None and path.is_relative_to(library.root):
            head = path.relative_to(library.root).parts[0]
            if head == ".recycle" or (len(head) == 32 and all(c in "0123456789abcdef" for c in head)):
                raise StorageError("请选择旧共享库或备份文件，当前作品可直接从列表打开。")
    assert_idle(knowledge, chat)
    backup = backup_pair(library, knowledge, chat)
    results = {"backup": str(backup), "imported": [], "skipped": [], "failed": []}
    with TemporaryDirectory(prefix=".migration-stage-", dir=library.root) as temporary:
        staging = Path(temporary).resolve()
        source_path = staging / BOOK_DB
        shutil.copyfile(backup / BOOK_DB, source_path)
        if chat:
            shutil.copyfile(backup / CHAT_DB, staging / CHAT_DB)
        with read_only(source_path) as conn:
            identity = conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='knowledge_identity'").fetchone()
            source_key = conn.execute("SELECT id FROM knowledge_identity").fetchone()[0] if identity else str(knowledge).casefold()
        source = Repository(Database(source_path))
        source_chat = ConversationService(source, staging / CHAT_DB)
        projects = source.projects()
        project_ids = {row["id"] for row in projects}
        with read_only(source_chat.db.path) as conn:
            bindings = conn.execute("SELECT library_id, project_id FROM sessions UNION SELECT library_id, project_id FROM project_guidance").fetchall()
        owned = {project for identity, project in bindings if identity == source_chat.library}
        if (bindings and not owned) or not owned.issubset(project_ids):
            raise StorageError("会话库与所选知识库的作品标识不匹配，请核对配套文件。")
        for project in projects:
            project_id, title = project["id"], project["name"]
            work = uuid5(NAMESPACE_URL, f"easy-writing/{source_key}/{project_id}").hex
            result = {"name": title, "work_id": work}
            try:
                if library.folder(work).exists() or library.folder(work, recycled=True).exists():
                    existing = library.info(work, recycled=library.folder(work, recycled=True).exists())
                    if existing["id"] != project_id or existing["origin"] != source_key:
                        raise StorageError("目标目录已被另一作品占用。")
                    results["skipped"].append(result)
                    continue

                def populate(repo, conversation):
                    copy_knowledge(source, repo, project_id)
                    copy_chat(source_chat, conversation, project_id)
                    verify_database(repo.db.path)
                    ChatDatabase.verify(conversation.db.path)
                    return project_id

                library.stage(work, populate, origin=source_key)
                results["imported"].append(result)
            except (StorageError, OSError, sqlite3.Error) as error:
                results["failed"].append(result | {"error": str(error) if isinstance(error, StorageError) else "复制或校验失败"})
    return results
