import json

import pytest

from easy_writing.retrieval.tools import citation_keys
from easy_writing.retrieval.workflow import ResearchWorkflow
from easy_writing.schemas import AssistantTurn, LLMResult, TokenUsage, ToolCall
from easy_writing.services.conversation import ChatRequest, ConversationService
from easy_writing.storage.database import StorageError
from easy_writing.storage.repository import uid
from easy_writing.ui.sample import import_sample
from test_timeline import book, raw_book


class Script:
    def __init__(self, *steps, answer=None):
        self.steps, self.answer, self.requests = steps, answer, []

    def call_tools(self, messages, tools, *, tool_choice="auto"):
        self.requests.append((messages, tools, tool_choice))
        index = len(self.requests) - 1
        packet = json.loads(messages[1]["content"])
        if index == 0:
            assert packet["strategy"] == "model_first"
            assert not (packet["records"] or packet["paths"] or packet["sources"])
        previous = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if index < len(self.steps) and tool_choice == "auto":
            name, args = self.steps[index]
            args = args(previous) if callable(args) else args
            turn = AssistantTurn(tool_calls=[ToolCall(id=uid(), name=name, arguments=args)])
        else:
            keys = sorted(set().union(*(citation_keys(result) for result in [packet, *previous])))
            result = self.answer or {"answer": "样稿答复", "citations": keys,
                                     "confidence": "supported" if keys else "insufficient"}
            turn = AssistantTurn(text=json.dumps(result, ensure_ascii=False))
        return LLMResult(content=turn, provider="fake", model="script", attempts=1, call_id=uid(),
                         usage=TokenUsage(prompt_tokens=180, completion_tokens=30, total_tokens=210))

    def close(self):
        pass


def run(book, client, question="第一章写了什么内容？", *, deep=False, pending=False, **scope):
    return ResearchWorkflow(book["repo"], client=client, model_first=True, deep=deep, allow_pending=pending,
                            max_llm_calls=5 if deep else 3, max_tool_calls=8 if deep else 3,
                            max_source_reads=4 if deep else 2).run(book["project"], question, **scope)


def test_model_first_calls_exact_chapter_without_a_lexical_gate(raw_book, monkeypatch):
    from easy_writing.retrieval.hybrid import HybridRetriever

    monkeypatch.setattr(HybridRetriever, "search", lambda *_a, **_kw: pytest.fail("模型选择前不应搜索"))
    client = Script(("get_chapter_overview", {"chapter": 1}))
    result = run(raw_book, client)
    refs = result["supplemental_context"][0]["records"]
    assert refs[0]["key"].startswith("summary:")
    assert {ref["chapter_no"] for record in refs for ref in record["evidence"]} == {1}
    assert result["answer"]["citations"] and result["metrics"]["llm_turns"] == 2
    assert result["metrics"]["total_tokens"] == 420 and result["metrics"]["source_read_calls"] == 0


def test_model_first_can_explain_missing_chapter_and_reject_scope_overrides(raw_book):
    client = Script(("get_chapter_overview", {"chapter": 20}), ("get_chapter_overview", {"chapter": 2}))
    result = run(raw_book, client, "第二章写了什么内容？")
    assert [step["status"] for step in result["tool_trace"]] == ["denied", "success"]
    assert result["supplemental_context"][0]["detail"]["status"] == "missing"
    assert not result["answer"]["citations"] and result["metrics"]["llm_turns"] == 3
    assert client.requests[-1][2] == "none"
    denied = Script(("get_chapter_overview", {"chapter": 1, "project_id": "other"}))
    result = run(raw_book, denied)
    assert result["tool_trace"][0]["status"] == "denied" and not result["supplemental_context"]


def test_character_details_returns_candidates_for_ambiguous_alias(raw_book):
    repo, project = raw_book["repo"], raw_book["project"]
    people = raw_book["knowledge"].characters(project)
    for person in people:
        repo.add_alias(person["id"], "阿林", "验收同名别称")
    client = Script(("get_character_details", {"character": "阿林"}),
                    answer={"answer": "你指的是林舟还是林月？", "citations": [], "confidence": "clarify"})
    result = run(raw_book, client, "请查阿林的身份")
    choices = result["supplemental_context"][0]
    assert choices["status"] == "ambiguous" and {p["name"] for p in choices["candidates"]} == {"林舟", "林月"}
    assert result["answer"]["confidence"] == "clarify"


def test_model_first_can_choose_event_chain_and_uses_conversation_viewpoint(tmp_path, recorder):
    from easy_writing.storage.database import Database
    from easy_writing.storage.repository import Repository

    repo = Repository(Database(tmp_path / "sample.db"))
    project = repo.create_project("模型先选工具验收")["id"]
    event = import_sample(repo, project, recorder)["event_id"]
    chat = ConversationService(repo)
    session = chat.create(project, "连续追问")

    def send(question, client, **scope):
        return chat.send(project, session, uid(), ChatRequest(question=question, **scope), client_factory=lambda: client,
                         model_first=True)["result"]

    event_lookup = Script(("get_event_details", {"event": "抵达旧宅"}),
                          ("get_related_events", lambda packets: {"event_id": packets[0]["detail"]["event_id"]}))
    first = send("林舟为什么去旧宅？", event_lookup, at_event=event)
    assert first["answer"]["citations"] and len(first["tool_trace"]) == 2
    character_lookup = Script(("get_character_details", {"character": "林舟", "topic": "地窖密码"}))
    follow = send("那时他知道密码吗？", character_lookup)
    assert follow["scope"]["actor"] and follow["scope"]["at_event"] == event
    assert "青石" not in json.dumps(follow, ensure_ascii=False)  # 保存的证据以编号定位
    assert follow["answer"]["citations"]
    tool_packet = json.loads(next(message["content"] for message in character_lookup.requests[-1][0] if message["role"] == "tool"))
    assert "青石" in tool_packet["records"][0]["text"]
    assert "林月尚未获知" not in json.dumps(tool_packet, ensure_ascii=False)
    visible = Script(("get_character_details", {"character": "林月"}))
    send("林月当时知道密码吗？", visible, actor=next(p["id"] for p in repo.known_entities(project) if p["name"] == "林月"))
    assert all("青石" not in json.dumps(messages, ensure_ascii=False) for messages, _, _ in visible.requests)


def test_source_requires_a_previous_tool_result_and_respects_read_budget(book):
    key = "fact:" + book["first"]
    reader = Script(("read_source", {"keys": [key]}),
                    ("get_chapter_overview", {"chapter": 1}))
    result = run(book, reader, "第一章发生了什么？")
    assert [step["status"] for step in result["tool_trace"]] == ["denied", "success"]
    assert result["metrics"]["source_read_calls"] == 0
    follow = Script(("get_chapter_overview", {"chapter": 1}),
                    ("read_source", lambda packets: {"keys": [packets[0]["records"][0]["key"]]}))
    result = run(book, follow, "第一章发生了什么？请核对原文。")
    assert result["metrics"]["source_read_calls"] == 1
    assert result["answer"]["citations"]


def test_model_first_pending_text_needs_explicit_authorization(book, recorder):
    from test_ingestion import pipeline

    with pytest.raises(StorageError):
        pipeline(book["repo"], recorder).ingest(book["project"], 1, "待入库", "墙缝暗号是白鹭。")
    operation = ("read_pending_source", {"chapter": 1})
    forbidden = run(book, Script(operation))
    assert not forbidden["supplemental_context"] and forbidden["metrics"]["source_read_calls"] == 0
    allowed = run(book, Script(operation), pending=True)
    source = allowed["supplemental_context"][0]["sources"][0]
    assert source["unindexed"] and source["chapter_no"] == 1 and "白鹭" in source["text"]
    assert allowed["metrics"]["source_read_calls"] == 1


def test_model_first_stops_after_duplicate_and_rejects_unseen_citations(book):
    step = ("get_chapter_overview", {"chapter": 1})
    client = Script(step, step, step, step, step, step)
    result = run(book, client, deep=True)
    assert result["metrics"]["stop_reason"] == "no_new_evidence"
    assert result["metrics"]["llm_turns"] <= 5 and result["metrics"]["tool_calls"] <= 8
    assert [row["status"] for row in result["tool_trace"]] == ["success", "denied", "denied"]
    bad = Script(answer={"answer": "虚构已确认结论", "citations": ["event:invented"], "confidence": "supported"})
    result = run(book, bad)
    assert result["metrics"]["stop_reason"] == "invalid_or_unseen_citation"
    assert "虚构" not in result["answer"]["answer"]


def test_model_first_does_not_serve_stale_snapshot(book):
    class Changed(Script):
        def call_tools(self, messages, tools, **options):
            response = super().call_tools(messages, tools, **options)
            book["repo"].edit("fact", book["first"], field="object_value", value="作者修订", reason="测试")
            return response

    with pytest.raises(StorageError, match="快照已过期"):
        run(book, Changed(("get_chapter_overview", {"chapter": 1})))
