import json
import sqlite3
from importlib.resources import files

import pytest

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.schemas import AssistantTurn, LLMResult, TokenUsage, ToolCall
from easy_writing.services.conversation import ChatRequest, ConversationService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid
from test_timeline import book, chapter_payload, raw_book


class ChatClient:
    def __init__(self, steps=()):
        self.steps = steps
        self.requests = []

    def call_tools(self, messages, tools, *, tool_choice="auto"):
        self.requests.append((messages, tool_choice))
        packet = json.loads(messages[1]["content"])
        index = len(self.requests) - 1
        if index < len(self.steps) and tool_choice == "auto":
            name, arguments = self.steps[index]
            turn = AssistantTurn(tool_calls=[ToolCall(id=f"tool_{index}", name=name, arguments=arguments)])
        else:
            supplemental = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            if any("已完成的补查" in message["content"] for message in messages if message["role"] == "user"):
                supplemental.insert(0, packet)
            links = [link["key"] for result in supplemental for path in result.get("paths", []) for link in path["links"]]
            citations = list(dict.fromkeys(links)) or [r["key"] for r in packet["records"][:1]] or [s["citation"] for s in packet["sources"][:1]]
            turn = AssistantTurn(text=json.dumps({"answer": "样稿验证答复", "citations": citations,
                                                  "confidence": "supported" if citations else "insufficient"}))
        return LLMResult(content=turn, provider="fake", model="scripted", usage=TokenUsage(), attempts=1, call_id=uid())

    def close(self):
        pass


def send(chat, project, session, question, *, client=None, turn_id=None, **options):
    client = client if client is not None else ChatClient()
    return chat.send(project, session, turn_id or uid(), ChatRequest(question=question, **options),
                     client_factory=lambda: client)["result"]


def test_chapter_overview_uses_exact_chapter_even_without_keyword_hits(raw_book):
    project = raw_book["project"]
    chat = ConversationService(raw_book["repo"])
    session = chat.create(project, "章节概述")
    client = ChatClient()
    result = send(chat, project, session, "第一章写了什么内容", client=client)
    assert result["retrieval"]["strategy"] == "chapter_overview"
    records = result["context"]["packet"]["records"]
    assert records[0]["key"].startswith("summary:")
    assert {ref["chapter_no"] for record in records for ref in record["evidence"]} == {1}
    assert len(client.requests) == 1 and result["answer"]["citations"]

    missing = ChatClient()
    result = send(chat, project, session, "第六章写了什么内容？", client=missing, through_chapter=None)
    assert not missing.requests and not result["context"]["packet"]["records"]
    assert "第 6 章" in result["answer"]["answer"]


def test_session_recovers_focus_and_author_goals_across_long_history(book):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session = chat.create(project, "钥匙线")
    first = send(chat, project, session, "林舟的身份与关系是什么？", goal="让交接更自然", constraints="保留回忆")
    for _ in range(7):
        send(chat, project, session, "下一步怎么写？")
    client = ChatClient()
    reopened = ConversationService(Repository(Database(repo.db.path)))
    result = send(reopened, project, session, "他后来参与了什么？", client=client)
    assert result["memory"]["focus_id"] == first["memory"]["focus_id"]
    sent = json.loads(client.requests[0][0][2]["content"])["会话记忆"]
    assert sent["goal"] == "让交接更自然" and sent["constraints"] == "保留回忆"
    assert len(sent["recent"]) <= 3
    assert any("林舟的身份" in row["问题"] for row in sent["summary"])
    changed = send(reopened, project, session, "请给写作建议，改成提前揭晓。", constraints="提前揭晓")
    assert changed["memory"]["constraints"] == "提前揭晓"
    assert changed["memory"]["directives"] == ["请给写作建议，改成提前揭晓。"]
    isolated = reopened.create(project, "独立会话")
    assert not send(reopened, project, isolated, "他后来参与了什么？")["memory"]["focus_id"]
    other = repo.create_project("另一本书")["id"]
    assert reopened.sessions(other) == []
    with pytest.raises(StorageError, match="不属于"):
        reopened.turns(other, session)


def test_follow_up_resolves_actor_and_filters_history_and_sources(book):
    repo, project, knowledge, events = (book[k] for k in ("repo", "project", "knowledge", "events"))
    knowledge.learn(book["secret"], "林舟", events[20], revealed_at_chapter=20, reason="作者核对")
    chat = ConversationService(repo)
    session = chat.create(project, "视角边界")
    send(chat, project, session, "林舟和地窖密码青石有什么关系？")
    follow = send(chat, project, session, "他在第五章已经知道密码吗？", at_event=events[5])
    assert follow["scope"]["actor"] == follow["memory"]["focus_id"]
    assert follow["scope"]["through_chapter"] == 5
    assert not follow["memory"]["recent"] and not follow["memory"]["summary"]
    assert "青石" not in json.dumps(follow, ensure_ascii=False)
    assert follow["metrics"]["source_read_calls"] == 0
    continued = send(chat, project, session, "他后来还知道什么？")
    assert continued["scope"] == follow["scope"]
    assert "青石" not in json.dumps(continued, ensure_ascii=False)


def test_at_that_time_uses_the_prior_named_event(book):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session = chat.create(project, "事件时点")
    send(chat, project, session, "林舟在取得钥匙时做了什么？")
    result = send(chat, project, session, "他当时知道密码吗？")
    assert result["scope"]["at_event"] == book["events"][1]
    assert result["scope"]["actor"] == result["memory"]["focus_id"]
    assert "青石" not in json.dumps(result["context"], ensure_ascii=False)


def test_alias_conflict_requires_clarification_then_accepts_explicit_focus(book):
    repo, project = book["repo"], book["project"]
    people = book["knowledge"].characters(project)
    for person in people:
        repo.add_alias(person["id"], "阿林", "样稿有两个同名别称")
    chat = ConversationService(repo)
    session = chat.create(project, "同名人物")
    client = ChatClient()
    unresolved = send(chat, project, session, "阿林是谁？", client=client)
    assert unresolved["metrics"]["stop_reason"] == "clarify" and not client.requests
    resolved = send(chat, project, session, "阿林是谁？", client=client, focus=people[0]["id"])
    assert resolved["memory"]["focus_id"] == people[0]["id"]
    assert any(row["key"] == "entity:" + people[0]["id"] for row in resolved["context"]["packet"]["records"])


def test_same_turn_is_idempotent_and_stale_history_is_excluded(book):
    repo, project = book["repo"], book["project"]
    chat = ConversationService(repo)
    session = chat.create(project, "改稿")
    turn = uid()
    client = ChatClient()
    first = send(chat, project, session, "钥匙由谁持有？", turn_id=turn, client=client)
    assert send(chat, project, session, "钥匙由谁持有？", turn_id=turn, client=client) == first
    assert len(client.requests) == 1 and len(chat.turns(project, session)) == 1
    repo.edit("fact", book["second"], field="object_value", value="新主人", reason="作者更正")
    assert chat.snapshot(project, None) != first["snapshot"]
    result = send(chat, project, session, "再查钥匙持有人")
    assert not result["memory"]["recent"]


def test_pending_text_requires_explicit_opt_in(book, recorder):
    from test_ingestion import pipeline

    repo, project = book["repo"], book["project"]
    with pytest.raises(StorageError):
        pipeline(repo, recorder).ingest(project, 1, "待入库新稿", "林舟发现墙缝暗号写着白鹭。")
    chat = ConversationService(repo)
    session = chat.create(project, "未入库边界")
    local = send(chat, project, session, "墙缝暗号是什么？", through_chapter=1)
    assert local["context"]["packet"]["coverage"]["not_current"] == [1]
    assert local["metrics"]["source_read_calls"] == local["metrics"]["llm_turns"] == 0
    client = ChatClient()
    opted = send(chat, project, session, "墙缝暗号是什么？", through_chapter=1, allow_pending=True, client=client)
    assert opted["context"]["packet"]["sources"][0]["unindexed"]
    assert "白鹭" in client.requests[0][0][1]["content"]
    assert "text" not in opted["context"]["packet"]["sources"][0]


def test_deep_mode_follows_multiple_hops_and_combines_citations(tmp_path, recorder):
    repo = Repository(Database(tmp_path / "chain.db"))
    project = repo.create_project("事件链")["id"]
    titles = ("铁门异响", "守卫离岗", "地窖渗水", "密信浮出", "港口截船", "船员逃亡", "旧案告破")
    for number, title in enumerate(titles, 1):
        payload = chapter_payload(title + "。", title)
        IngestionPipeline(repo, FixtureLLMClient(recorder, {"b0001": payload}), model_identity="chat-chain").ingest(project, number, title, title + "。")
    events = {e["chapter_no"]: e["id"] for e in repo.records(project, "event")}
    graph = TimelineService(repo)
    for i in range(1, len(events)):
        graph.link(events[i], events[i + 1], "causes", revealed_at_chapter=i + 1, reason="样稿因果关系")
    chat = ConversationService(repo)
    session = chat.create(project, "多跳核查")
    steps = [("get_related_events", {"event_id": events[i], "direction": "out", "relation": "causes"}) for i in (1, 3, 5)]
    normal = send(chat, project, session, "铁门异响", client=ChatClient(steps))
    assert normal["metrics"]["llm_turns"] == 3
    deep = send(chat, project, chat.create(project, "深度核查"), "铁门异响", client=ChatClient(steps), deep=True)
    assert deep["metrics"]["llm_turns"] == 4
    assert deep["metrics"]["tool_calls"] == 3
    assert deep["metrics"]["source_read_calls"] == 0
    assert deep["metrics"]["estimated_cumulative_input"] <= 48000
    assert any(item["trigger"] == "preflight" for item in deep["metrics"]["compactions"])
    assert len(deep["answer"]["citations"]) == 6
    assert len(normal["answer"]["citations"]) < len(deep["answer"]["citations"])
    assert all(key.startswith("link:") for key in deep["answer"]["citations"])


def test_deep_mode_finalizes_when_research_has_no_new_evidence(book):
    chat = ConversationService(book["repo"])
    session = chat.create(book["project"], "收尾")
    client = ChatClient([("get_ingestion_status", {})] * 10)
    result = send(chat, book["project"], session, "钥匙持有人", client=client, deep=True)
    assert [choice for _, choice in client.requests] == ["auto", "none"]
    assert result["metrics"]["stop_reason"] == "no_new_evidence"
    assert result["answer"]["confidence"] == "supported"


def test_v6_upgrade_preserves_old_task_and_backup(tmp_path):
    path = tmp_path / "v6.db"
    with sqlite3.connect(path) as conn:
        for version in range(1, 7):
            name = "schema.sql" if version == 1 else f"migration_{version:03d}.sql"
            conn.executescript(files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8"))
        conn.execute("PRAGMA user_version=6")
        conn.execute("INSERT INTO projects VALUES('old','旧项目','old')")
        conn.execute("INSERT INTO work_tasks VALUES('old-task','old','research','hash','succeeded','{}',NULL,'old','old')")
    db = Database(path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION
        assert conn.execute("SELECT id FROM work_tasks").fetchone()[0] == "old-task"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(str(path) + ".pre-v7.bak") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
