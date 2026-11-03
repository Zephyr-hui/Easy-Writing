from pathlib import Path

import pytest

from easy_writing.desktop.backend import DesktopBackend
from easy_writing.desktop.library import BOOK_DB, CHAT_DB, WorkLibrary
from easy_writing.desktop import library_migration
from easy_writing.services.archive import file_hash
from easy_writing.services.checking import CheckingService
from easy_writing.services.conversation import ChatRequest
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import now
from easy_writing.ui.clients import recorder
from easy_writing.ui.sample import import_sample


def add_turn(backend, chat, project, session):
    result = backend.ask(chat, project, session, ChatRequest(question="第一章写了什么内容？"))
    assert result["answer"]["citations"]
    with chat.db.transaction() as conn:
        seq, scope, topic = conn.execute("""SELECT t.seq,m.scope_key,m.topic FROM turns t
            JOIN turn_memory m ON m.turn_seq=t.seq WHERE t.library_id=? AND t.session_id=?""",
                                         (chat.library, session)).fetchone()
        conn.execute("INSERT OR REPLACE INTO rolling_memory VALUES(?,?,?,?,?,?)", (chat.library, session, scope, topic, seq, "{}"))
        conn.execute("INSERT INTO summary_attempts VALUES(?,?,?,'succeeded','{}')", (session, seq, now()))
    return seq


def test_work_lifecycle_and_exact_session_deletion(tmp_path):
    backend = DesktopBackend(tmp_path)
    root = tmp_path / "works"
    with WorkLibrary(root) as library:
        with pytest.raises(StorageError, match="另一桌面窗口"):
            WorkLibrary(root)
        first = library.create("作品", initialize=lambda repo, project: import_sample(repo, project, recorder(backend.settings)))
        second = library.create("作品")
        a, b = first["work_id"], second["work_id"]
        assert a != b
        with pytest.raises(StorageError, match="标识无效"):
            library.delete("..")
        project = first["id"]
        repo, chat = library.open(a)
        other_repo, other_chat = library.open(b)
        assert chat.library != other_chat.library
        assert all((row["folder"] / name).is_file() for row in (first, second) for name in (BOOK_DB, CHAT_DB))
        assert other_repo.chapters(second["id"]) == []
        remove, keep = [chat.create(project, "同名会话") for _ in range(2)]
        removed_seq = add_turn(backend, chat, project, remove)
        add_turn(backend, chat, project, keep)
        chat.rename(project, keep, "继续写")
        assert {r["id"]: r["title"] for r in chat.sessions(project)}[keep] == "继续写"
        with chat.db.transaction() as conn:
            conn.execute("UPDATE summary_attempts SET status='running' WHERE turn_seq=?", (removed_seq,))
        with pytest.raises(StorageError, match="摘要"):
            chat.delete(project, remove)
        with pytest.raises(StorageError, match="摘要"):
            library.delete(a)
        with chat.db.transaction() as conn:
            conn.execute("UPDATE summary_attempts SET status='succeeded' WHERE turn_seq=?", (removed_seq,))
        chat.delete(project, remove)
        with chat.db.connect() as conn:
            for table in ("sessions", "turns", "turn_memory", "rolling_memory", "summary_attempts"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
            assert not conn.execute("SELECT 1 FROM turn_memory WHERE turn_seq=?", (removed_seq,)).fetchone()
        assert chat.turns(project, keep) and repo.chapters(project)
        library.rename(a, "长篇新名")
        assert library.info(a)["folder"] == first["folder"]
        library.delete(a)
        assert [row["work_id"] for row in library.works()] == [b]
        assert not first["folder"].exists()
        assert library.info(a, recycled=True)["name"] == "长篇新名"
        library.restore(a)
    with WorkLibrary(root) as reopened:
        assert reopened.info(a)["name"] == "长篇新名"
        assert reopened.open(a)[1].turns(project, keep)
        assert reopened.open(b)[1].sessions(second["id"]) == []


def test_migration_splits_shared_pair_preserving_ids_and_retries(tmp_path, monkeypatch):
    backend = DesktopBackend(tmp_path / "legacy")
    source, source_chat = backend.open()
    old = {}
    for name in ("第一部", "第二部"):
        project = source.create_project(name)["id"]
        import_sample(source, project, recorder(backend.settings))
        CheckingService(source).run(project)
        session = source_chat.create(project, "同名会话")
        add_turn(backend, source_chat, project, session)
        old[name] = project, session
    source_paths = (source.db.path, source_chat.db.path)
    hashes = [file_hash(path) for path in source_paths]

    copy_chat = library_migration.copy_chat

    def fail_one(original, target, project):
        if project == old["第一部"][0]:
            raise StorageError("模拟单部作品复制失败")
        copy_chat(original, target, project)

    with WorkLibrary(tmp_path / "works") as library:
        with monkeypatch.context() as patch:
            patch.setattr(library_migration, "copy_chat", fail_one)
            result = library_migration.migrate(library, *source_paths)
        assert [r["name"] for r in result["failed"]] == ["第一部"]
        assert [r["name"] for r in library.works()] == ["第二部"]
        result = library_migration.migrate(library, *source_paths)
        assert [r["name"] for r in result["imported"]] == ["第一部"], result
        assert [r["name"] for r in result["skipped"]] == ["第二部"]
        assert (Path(result["backup"]) / "checksums.json").is_file()
        for row in library.works():
            project, session = old[row["name"]]
            repo, chat = library.open(row["work_id"])
            assert [r["id"] for r in repo.projects()] == [project]
            assert [r["id"] for r in chat.sessions(project)] == [session]
            assert chat.library != source_chat.library
            assert repo.chapters(project) == source.chapters(project)
            assert chat.turns(project, session) == source_chat.turns(project, session)
            assert chat.snapshot(project, 1) == source_chat.snapshot(project, 1)
            with repo.db.connect() as conn:
                assert conn.execute("SELECT COUNT(*) FROM check_runs").fetchone()[0] == 1
        removed = library.works()[0]["work_id"]
        library.delete(removed)
        result = library_migration.migrate(library, *source_paths)
        assert not result["imported"] and len(result["skipped"]) == 2
        assert not library.folder(removed).exists()
    assert [file_hash(path) for path in source_paths] == hashes
