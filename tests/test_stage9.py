import json
import sqlite3

import httpx
import pytest

from easy_writing.llm.deepseek import DeepSeekClient
from easy_writing.llm.base import LLMError
from easy_writing.retrieval.compaction import compact_messages, request_size
from easy_writing.retrieval.tools import citation_keys
from easy_writing.schemas import LLMResult, TokenUsage
from easy_writing.services.archive import ArchiveService
from easy_writing.services.chat_memory import encode
from easy_writing.services.conversation import ConversationService
from easy_writing.storage.chat_database import ChatDatabase
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid
from test_conversation import ChatClient, send
from test_timeline import book, raw_book


def legacy_rows(repo, project):
    session, turn = uid(), uid()
    result = {"answer": {"answer": "旧回复", "citations": ["event:old"], "confidence": "supported"},
              "scope": {"actor": None, "through_chapter": None, "at_event": None}, "snapshot": "old-snapshot", "memory": {},
              "context": {"packet": {"records": [{"key": "event:old", "text": "旧正文",
                                                  "evidence": [{"version_id": "old", "quote": "旧正文"}]}]}}}
    with repo.db.transaction() as conn:
        conn.execute("INSERT INTO chat_sessions VALUES(?,?,?,?)", (session, project, "旧会话", "old"))
        conn.execute("INSERT INTO work_tasks VALUES(?,?,'research','oldhash','succeeded',?,NULL,'old','old')",
                     (turn, project, encode(result)))
        conn.execute("INSERT INTO chat_turns VALUES(1,?,?,?)", (session, turn, encode({"question": "旧问题"})))
        conn.execute("INSERT INTO work_tasks VALUES('standalone',?,'research','other','succeeded','{}',NULL,'old','old')",
                     (project,))
    return session, turn


def test_legacy_migration_keeps_only_chat_data_in_chat_file_and_can_reopen(tmp_path):
    repo = Repository(Database(tmp_path / "novel.db"))
    project = repo.create_project("迁移")["id"]
    session, turn = legacy_rows(repo, project)
    with repo.db.transaction() as conn:
        conn.execute("DROP TABLE chapter_drafts")
        conn.execute("DROP TABLE chapter_file_sources")
        conn.execute("DROP TABLE knowledge_identity")
        conn.execute("PRAGMA user_version=7")
    repo = Repository(Database(repo.db.path))
    chat = ConversationService(repo)
    moved = chat.turns(project, session)[0]
    assert moved["id"] == turn and moved["request"]["question"] == "旧问题"
    assert moved["result"]["answer"]["answer"] == "旧回复"
    assert moved["result"]["answer"]["citations"] == ["event:old"]
    assert "旧正文" not in encode(moved)
    assert moved["summary"]["turn"] == turn
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT id FROM work_tasks").fetchone()[0] == "standalone"
    with chat.db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='chapters'").fetchone()
    original_backup = next(tmp_path.glob("novel.db.pre-chat-split-*.bak"))
    with sqlite3.connect(original_backup) as conn:
        assert conn.execute("SELECT task_id FROM chat_turns").fetchone()[0] == turn
    assert ConversationService(repo).turns(project, session) == [moved]
    with pytest.raises(StorageError, match="知识库"):
        Database(chat.db.path)


def test_migration_conflict_keeps_source_and_can_retry(tmp_path):
    repo = Repository(Database(tmp_path / "novel.db"))
    project = repo.create_project("迁移冲突")["id"]
    session, _ = legacy_rows(repo, project)
    chat_db = ChatDatabase(tmp_path / "chat.db")
    with repo.db.connect() as conn:
        library = conn.execute("SELECT id FROM knowledge_identity").fetchone()[0]
    with chat_db.transaction() as conn:
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (library, session, project, "冲突", "old"))
    with pytest.raises(StorageError, match="冲突"):
        ConversationService(repo, chat_db.path)
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0] == 1
    with chat_db.transaction() as conn:
        conn.execute("UPDATE sessions SET title='旧会话'")
    assert len(ConversationService(repo, chat_db.path).turns(project, session)) == 1


def test_independent_archives_preserve_binding_and_reject_wrong_file(book, tmp_path):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session = chat.create(project, "备份")
    send(chat, project, session, "钥匙持有人")
    knowledge_backup, chat_backup = tmp_path / "knowledge.bak", tmp_path / "chat.bak"
    ArchiveService(repo).backup(knowledge_backup)
    chat.db.archive().backup(chat_backup)
    restored_knowledge, restored_chat = tmp_path / "restored.db", tmp_path / "restored-chat.db"
    ArchiveService(repo).restore(knowledge_backup, restored_knowledge)
    chat.db.archive().restore(chat_backup, restored_chat)
    restored = ConversationService(Repository(Database(restored_knowledge)), restored_chat)
    assert len(restored.turns(project, session)) == 1
    separate = Repository(Database(tmp_path / "separate.db"))
    with separate.db.transaction() as conn:
        conn.execute("INSERT INTO projects VALUES(?,?,?)", (project, "不同知识库", "now"))
    assert ConversationService(separate, restored_chat).sessions(project) == []
    with pytest.raises(StorageError):
        chat.db.archive().restore(knowledge_backup, tmp_path / "wrong.db")


def test_semantic_summary_failure_keeps_answer_and_can_retry(book):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session, turn = chat.create(project, "摘要"), uid()

    class Summarizer(ChatClient):
        def extract_json(self, prompt, schema, *, system_prompt):
            raise LLMError("超时", code="timeout")

    result = send(chat, project, session, "请提供钥匙情节的写作建议", turn_id=turn, client=Summarizer(),
                  semantic_summary=True, goal="处理钥匙交接")
    failed = chat.turns(project, session)[0]
    assert failed["summary_status"] == "failed" and failed["result"] == result
    assert failed["summary"]["goal"] == "处理钥匙交接"

    class Recovered(ChatClient):
        def extract_json(self, prompt, schema, *, system_prompt):
            return LLMResult(content=schema(proposals=["提前交接"], unresolved=["谁应在场"]),
                             provider="fake", model="scripted", usage=TokenUsage(total_tokens=120), attempts=1, call_id=uid())

    chat.summarize(project, session, turn, client_factory=Recovered)
    recovered = chat.turns(project, session)[0]
    assert recovered["summary_status"] == "succeeded"
    assert recovered["summary"]["unresolved"] == ["谁应在场"]
    assert recovered["summary_usage"]["usage"]["total_tokens"] == 120
    later = send(chat, project, session, "钥匙交接怎么写？")
    assert later["memory"]["rolling"][0]["unresolved"] == [{"text": "谁应在场", "turn": turn}]
    chat.summarize(project, session, turn, client_factory=lambda: pytest.fail("不应重复调用"))


def test_provider_overflow_retries_once_with_smaller_context(book, settings, recorder):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session = chat.create(project, "超限")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "context_length_exceeded"}})
        packet = json.loads(body["messages"][1]["content"])
        answer = {"answer": "压缩后的回复", "citations": [packet["records"][0]["key"]], "confidence": "supported"}
        return httpx.Response(200, json={"choices": [{"message": {"content": encode(answer)}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = DeepSeekClient(settings, recorder, http_client=http)
        result = send(chat, project, session, "请说明林舟和林月的钥匙关系，给出写作建议", client=client,
                      goal="保持交接可信", constraints="不提前揭晓密码")
    metrics = result["metrics"]
    assert metrics["llm_turns"] == len(requests) == 2 and metrics["context_retry"]
    assert metrics["compactions"][0]["after"] < metrics["compactions"][0]["before"]
    assert request_size(requests[1]["messages"], requests[1]["tools"], settings.max_output_tokens) < request_size(
        requests[0]["messages"], requests[0]["tools"], settings.max_output_tokens)
    assert "不提前揭晓密码" in requests[1]["messages"][2]["content"]
    assert result["answer"]["answer"] == "压缩后的回复"
    assert [entry["status"] for entry in recorder.events[-2:]] == ["context_length", "success"]


def test_compaction_deduplicates_sources_and_preserves_visible_citations():
    record = {"key": "event:a", "text": "关键证据" * 100, "text_window": [0, 400, 400], "evidence": []}
    node = {"key": "event:a", "title": "关键事件"}
    link = {"id": "L", "key": "link:L", "source": "a", "target": "b", "type": "before"}
    source = {"citation": "event:a", "version_id": "v", "paragraph_id": "p", "text": "局部原文" * 100}
    packet = {"question": "核查", "records": [record], "sources": [source],
              "paths": [{"root": "a", "nodes": [node, {"key": "event:b", "title": "后续"}], "links": [link]}]}
    messages, compact = compact_messages("规则", packet, [packet, packet], {"goal": "写法", "constraints": "保留伏笔"}, [], 2)
    assert len(compact["records"]) == len(compact["sources"]) == len(compact["paths"][0]["links"]) == 1
    assert citation_keys(compact) == {"event:a", "event:b", "link:L"}
    assert request_size(messages, [], 512) < request_size([packet, packet, packet], [], 512)
    assert "保留伏笔" in messages[2]["content"]
