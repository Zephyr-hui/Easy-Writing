import json
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from test_timeline import book, raw_book
from easy_writing.cli import main
from easy_writing.retrieval.context import ContextBuilder, estimate_tokens
from easy_writing.retrieval.corpus import CorpusBuilder
from easy_writing.retrieval.embeddings import BGEEmbeddingProvider, FakeEmbeddingProvider
from easy_writing.retrieval.hybrid import HybridRetriever
from easy_writing.retrieval.index import VectorIndex
from easy_writing.retrieval.sources import SourceReader
from easy_writing.retrieval.tools import ReadOnlyTools
from easy_writing.retrieval.workflow import DemoResearchClient, ResearchWorkflow
from easy_writing.schemas import AssistantTurn, LLMResult, TokenUsage, ToolCall
from easy_writing.storage.database import StorageError
from easy_writing.usage import JsonlCallRecorder


class MeaningFixture(FakeEmbeddingProvider):
    model_name, revision, dimension, max_tokens = "test-meaning-fixture", "v1", 3, 64

    def encode(self, texts, *, query=False):
        result = []
        for text in texts:
            if "密码" in text or "口令" in text:
                result.append([0, 1, 0])
            elif "钥匙" in text or "开门" in text or "银色锁片" in text:
                result.append([1, 0, 0])
            else:
                result.append([0, 0, 1])
        return np.asarray(result, dtype=np.float32)


def corpus_for(b, **scope):
    return CorpusBuilder(b["repo"]).build(b["project"], **scope)


def test_embedding_splits_without_losing_tail_or_exceeding_limit():
    provider = MeaningFixture()
    text = "甲"*400 + "银色锁片"
    parts = provider.split(text)
    assert "".join(p.text for p in parts) == text
    assert all(p.token_count <= provider.max_tokens for p in parts)
    assert parts[-1].end == len(text)
    with pytest.raises(StorageError, match="超过"):
        provider.checked_encode([text], query=True)


def test_invalid_embedding_shape_or_zero_vector_is_rejected():
    provider = MeaningFixture()
    provider.encode = lambda texts, query=False: np.zeros((len(texts), 3))
    with pytest.raises(StorageError, match="零向量"):
        provider.checked_encode(["test"])
    provider.encode = lambda texts, query=False: np.ones((len(texts), 2))
    with pytest.raises(StorageError, match="维度"):
        provider.checked_encode(["test"])


def test_index_is_incremental_and_model_versions_never_mix(book):
    corpus = corpus_for(book)
    provider = MeaningFixture()
    first = VectorIndex(book["repo"], provider).ensure(corpus)
    assert first["new_segments"] > 0
    assert VectorIndex(book["repo"], provider).ensure(corpus)["new_segments"] == 0
    old_fingerprint = provider.fingerprint
    provider.revision = "v2"
    second = VectorIndex(book["repo"], provider).ensure(corpus)
    assert second["new_segments"] == first["new_segments"]
    assert provider.fingerprint != old_fingerprint


def test_paraphrase_semantic_channel_can_recall_key_state(book):
    corpus = corpus_for(book, at_event=book["events"][5])
    result = HybridRetriever(book["repo"], MeaningFixture()).search(corpus, "开门的东西归谁保管", limit=8)
    hit = next(h for h in result["hits"] if h["record"].id == book["second"])
    assert hit["channels"]["semantic"] > 0.9
    assert len({h["record"].key for h in result["hits"]}) == len(result["hits"])


def test_edited_text_ignores_old_vector_without_deleting_history(book):
    repo = book["repo"]
    provider = MeaningFixture()
    old = corpus_for(book)
    VectorIndex(repo, provider).ensure(old)
    repo.edit("fact", book["second"], field="object_value", value="新主人", reason="作者修正")
    fresh = corpus_for(book)
    with pytest.raises(StorageError, match="过期"):
        CorpusBuilder(repo).check(old)
    result = HybridRetriever(repo, provider).search(fresh, "钥匙持有人", limit=20)
    assert any(h["record"].text.endswith("新主人") for h in result["hits"] if h["record"].id == book["second"])
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(DISTINCT source_hash) FROM vector_segments WHERE record_id=?", (book["second"],)).fetchone()[0] == 2


def test_rejected_fact_and_stale_summary_are_not_in_corpus(book):
    book["repo"].edit("fact", book["second"], status="rejected", reason="误提取")
    corpus = corpus_for(book)
    assert book["second"] not in {r.id for r in corpus.records}
    assert not any(r.kind == "summary" and r.generation_id == book["repo"].job(book["repo"].chapters(book["project"])[1]["published_generation_id"])["id"] for r in corpus.records)


def test_future_information_filtered_before_semantic_ranking(book):
    corpus = corpus_for(book, through_chapter=10)
    result = HybridRetriever(book["repo"], MeaningFixture()).search(corpus, "口令是什么", limit=20)
    serialized = json.dumps([h["record"].text for h in result["hits"]], ensure_ascii=False)
    assert "青石" not in serialized
    assert book["secret"] not in {r.id for r in corpus.records}


def test_actor_scope_strips_overbroad_quotes_and_denies_raw(book):
    k = book["knowledge"]
    k.learn(book["secret"], "林月", book["events"][10], revealed_at_chapter=20, reason="记录知晓")
    corpus = corpus_for(book, actor="林月", at_event=book["events"][10])
    # 原文证据中有额外旁白，资料包只返回被授权的命题，不返回原文 quote。
    hits = HybridRetriever(book["repo"], MeaningFixture()).search(corpus, "口令", limit=5)["hits"]
    packet = ContextBuilder(6000).build(corpus, "口令", hits)["packet"]
    assert packet["records"] and all("quote" not in ref for r in packet["records"] for ref in r["evidence"])
    with pytest.raises(StorageError, match="角色认知"):
        SourceReader(book["repo"], corpus).read([hits[0]["record"].key])


def test_context_budget_and_long_record_window_are_explicit(book):
    repo = book["repo"]
    repo.edit("fact", book["second"], field="object_value", value="甲"*1000 + "银色锁片", reason="长记录")
    corpus = corpus_for(book, subject="钥匙", at_event=book["events"][5])
    hits = HybridRetriever(repo, MeaningFixture()).search(corpus, "银色锁片", limit=5)["hits"]
    result = ContextBuilder(1500).build(corpus, "银色锁片", hits)
    assert result["estimated_tokens"] == estimate_tokens(result["packet"]) <= 1500
    assert "银色锁片" in json.dumps(result["packet"], ensure_ascii=False)
    assert result["packet"]["truncated"]


def test_source_reader_checks_versions_and_budget(book):
    corpus = corpus_for(book)
    key = "fact:" + book["second"]
    reader = SourceReader(book["repo"], corpus, max_reads=1, max_chars=30)
    result = reader.read([key], version_id=next(r for r in corpus.records if r.key == key).evidence[0]["version_id"])
    assert result and sum(len(r["text"]) for r in result) <= 30
    with pytest.raises(StorageError, match="预算"):
        reader.read([key])
    with pytest.raises(StorageError, match="范围"):
        SourceReader(book["repo"], corpus).read(["fact:other-project"])
    with pytest.raises(StorageError, match="版本"):
        SourceReader(book["repo"], corpus).read([key], version_id="wrong-version")


def test_unknown_or_future_event_chain_does_not_expand(book):
    corpus = corpus_for(book, at_event=book["events"][5])
    with pytest.raises(StorageError, match="时点"):
        SourceReader(book["repo"], corpus).paths([book["events"][10]])


def test_tool_white_list_and_scope_cannot_be_overridden(book):
    corpus = corpus_for(book, through_chapter=10)
    retriever = HybridRetriever(book["repo"], MeaningFixture())
    tools = ReadOnlyTools(corpus, retriever, SourceReader(book["repo"], corpus), "查询")
    with pytest.raises(StorageError, match="白名单"):
        tools.execute("delete_database", {})
    with pytest.raises(StorageError, match="参数"):
        tools.execute("search_memory", {"query": "口令", "actor": None, "project_id": "other"})
    result = tools.execute("search_memory", {"query": "口令"})
    assert "青石" not in json.dumps(result, ensure_ascii=False)


def test_source_pending_reads_only_latest_saved_unindexed_version(book, recorder):
    from test_ingestion import pipeline
    new = "林舟在墙缝发现暗号：白鹭。"
    with pytest.raises(StorageError):
        pipeline(book["repo"], recorder).ingest(book["project"], 1, "新稿", new)
    workflow = ResearchWorkflow(book["repo"], None, context_budget=6000)
    result = workflow.run(book["project"], "墙缝的暗号是什么", through_chapter=1)
    sources = result["context"]["packet"]["sources"]
    assert sources and sources[0]["unindexed"]
    assert "白鹭" in sources[0]["text"]
    assert "青石" not in json.dumps(result, ensure_ascii=False)
    assert not book["repo"].chapters(book["project"])[0]["knowledge_is_current"]


def test_ordinary_node_remains_searchable_and_path_is_preserved(book):
    b = book
    plot = b["graph"].create_plot(b["project"], "检索主线")["id"]
    b["graph"].set_role(plot, b["events"][20], "ordinary", reason="普通细节")
    corpus = corpus_for(book)
    hits = HybridRetriever(b["repo"], None).search(corpus, "回忆修复", limit=10)["hits"]
    assert any(h["record"].id == b["events"][20] for h in hits)
    paths = SourceReader(b["repo"], corpus).paths([b["events"][5]])
    assert any(n["key"] == "event:"+b["events"][20] for p in paths for n in p["nodes"])


class ScriptedClient:
    def __init__(self, *, always_call=False, bad_citation=False):
        self.calls = 0
        self.always_call, self.bad_citation = always_call, bad_citation

    def call_tools(self, messages, tools, **kwargs):
        from langsmith.run_helpers import get_tracing_context
        assert get_tracing_context().get("enabled") is False
        self.calls += 1
        packet = json.loads(messages[1]["content"])
        key = packet["records"][0]["key"]
        if self.calls == 1 or self.always_call:
            turn = AssistantTurn(tool_calls=[ToolCall(id=f"call{self.calls}", name="read_source", arguments={"keys": [key]})])
        else:
            assert messages[-1]["role"] == "tool" and messages[-1]["tool_call_id"] == "call1"
            turn = AssistantTurn(text=json.dumps({"answer": "按已给资料回答", "citations": ["fact:invented" if self.bad_citation else key], "confidence": "supported"}))
        return LLMResult(content=turn, provider="fake", model="scripted", usage=TokenUsage(), attempts=1, call_id=uuid4().hex)


def test_langgraph_native_tool_loop_and_citations(book):
    client = ScriptedClient()
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=client).run(book["project"], "钥匙")
    assert client.calls == 2
    assert result["tool_trace"][-1]["status"] == "success"
    assert result["answer"]["confidence"] == "supported"
    assert result["supplemental_context"][0]["sources"]
    assert result["metrics"]["real_model_calls"] == 0


def test_model_loop_and_source_reads_are_bounded(book):
    client = ScriptedClient(always_call=True)
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=client, max_llm_calls=2, max_tool_calls=1, max_source_reads=1).run(book["project"], "钥匙")
    assert client.calls == 2
    assert result["metrics"]["source_read_calls"] == 1
    assert result["metrics"]["stop_reason"] == "llm_budget"


def test_never_source_mode_applies_to_model_tool_calls(book):
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=ScriptedClient(), source_mode="never").run(book["project"], "钥匙")
    assert result["metrics"]["source_read_calls"] == 0
    assert result["tool_trace"][0]["status"] == "denied"


def test_unseen_citation_does_not_become_success(book):
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=ScriptedClient(bad_citation=True)).run(book["project"], "钥匙")
    assert result["metrics"]["stop_reason"] == "invalid_or_unseen_citation"
    assert result["answer"]["confidence"] == "insufficient"


def test_input_budget_stops_before_model_request(book):
    client = ScriptedClient()
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=client, input_budget=1000).run(book["project"], "钥匙")
    assert client.calls == 0
    assert result["metrics"]["stop_reason"] == "input_budget"


def test_metadata_logs_exclude_question_context_and_credentials(book, tmp_path):
    path = tmp_path / "metrics.jsonl"
    result = ResearchWorkflow(book["repo"], None, recorder=JsonlCallRecorder(path)).run(book["project"], "钥匙-PRIVATE-QUESTION")
    log = path.read_text(encoding="utf-8")
    assert "PRIVATE-QUESTION" not in log and "林舟" not in log
    assert json.loads(log)["context_estimated_tokens"] == result["metrics"]["context_estimated_tokens"]


def test_cli_index_search_and_offline_research(book, capsys):
    db, project = str(book["repo"].db.path), book["project"]
    def call(args):
        assert main([*args, "--db", db]) == 0
        return json.loads(capsys.readouterr().out)
    assert call(["index", "--project", project, "--embedding", "fake"])["new_segments"] > 0
    assert call(["index", "--project", project, "--embedding", "fake"])["new_segments"] == 0
    result = call(["search", "--project", project, "--question", "第十章口令是什么", "--embedding", "fake"])
    assert "青石" not in json.dumps(result, ensure_ascii=False)
    result = call(["research", "--project", project, "--question", "钥匙", "--embedding", "fake", "--provider", "fake"])
    assert result["answer"]["answer"].startswith("【离线流程演示】")


def test_concurrent_edit_during_embedding_rejects_snapshot_and_logs_failure(book, tmp_path):
    provider = MeaningFixture()
    original = provider.encode
    changed = False
    def encode(texts, *, query=False):
        nonlocal changed
        if not changed:
            changed = True
            book["repo"].edit("fact", book["second"], field="object_value", value="已改稿", reason="模拟并发修改")
        return original(texts, query=query)
    provider.encode = encode
    path = tmp_path / "failed.jsonl"
    with pytest.raises(StorageError, match="过期"):
        ResearchWorkflow(book["repo"], provider, recorder=JsonlCallRecorder(path)).run(book["project"], "PRIVATE查询钥匙")
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["status"] == "failed" and row["usage_incomplete"]
    assert "PRIVATE" not in path.read_text(encoding="utf-8")


def test_actor_workflow_cannot_receive_unrelated_narrator_text(book):
    book["knowledge"].learn(book["secret"], "林月", book["events"][10], reason="获知密码", revealed_at_chapter=20)
    result = ResearchWorkflow(book["repo"], MeaningFixture(), client=ScriptedClient()).run(
        book["project"], "林月知道的口令", actor="林月", at_event=book["events"][10])
    assert "修复" not in json.dumps(result, ensure_ascii=False)
    assert result["metrics"]["source_read_calls"] == 0


def test_fake_demo_script_requires_no_model_downloads_or_keys(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts/demo_stage4.py"), "--embedding", "fake", "--db", str(tmp_path / "demo/novel.db")],
                            capture_output=True, text=True, encoding="utf-8", check=True)
    report = json.loads(result.stdout)
    assert report["real_model_calls"] == 0
    assert report["reader_boundary_verified"] and report["long_text_preserved"]


def test_pending_source_can_be_followed_up_without_publishing_knowledge(book, recorder):
    from test_ingestion import pipeline
    text = "林舟发现墙缝暗号。\n暗号写着白鹭，旁边画着渡口。"
    with pytest.raises(StorageError):
        pipeline(book["repo"], recorder).ingest(book["project"], 1, "待入库新稿", text)
    corpus = corpus_for(book, through_chapter=1)
    source = SourceReader(book["repo"], corpus, max_reads=2)
    initial = source.pending("墙缝暗号")
    follow = source.read([initial[0]["citation"]], version_id=initial[0]["version_id"])
    assert follow and all(s["unindexed"] for s in follow)
    assert "白鹭" in json.dumps(follow, ensure_ascii=False)
    assert not book["repo"].chapters(book["project"])[0]["knowledge_is_current"]


def test_overlong_query_is_rejected_before_index_work(book):
    corpus = corpus_for(book)
    with pytest.raises(StorageError, match="缩短"):
        HybridRetriever(book["repo"], MeaningFixture()).search(corpus, "甲"*500)
    with book["repo"].db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vector_segments").fetchone()[0] == 0


def test_arbitrary_local_folder_is_not_treated_as_a_model(tmp_path):
    (tmp_path / ".env").write_text("private", encoding="utf-8")
    with pytest.raises(StorageError, match="加载失败"):
        BGEEmbeddingProvider(str(tmp_path))
