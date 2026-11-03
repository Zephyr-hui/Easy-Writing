import json
import sqlite3
from importlib.resources import files

import pytest

from easy_writing.cli import main
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository


def chapter_payload(text, event_title, *, holder=None, secret=False, recollection=False):
    evidence = [{"paragraph_id": "p0001", "quote": text}]
    entities = [
        {"key": "p1", "name": "林舟", "kind": "person", "evidence": evidence},
        {"key": "p2", "name": "林月", "kind": "person", "evidence": evidence},
        {"key": "key1", "name": "钥匙", "kind": "item", "evidence": evidence},
        {"key": "room", "name": "地窖", "kind": "place", "evidence": evidence},
    ]
    facts = []
    if holder:
        facts.append({"key": "f1", "subject": "key1", "predicate": "持有人", "object_value": "林舟" if holder == "p1" else "林月", "object_entity": holder, "effective_from": "e1", "evidence": evidence})
    if secret:
        facts.append({"key": "secret", "subject": "room", "predicate": "密码", "object_value": "青石", "evidence": evidence})
    if holder == "p1":
        facts.append({"key": "relation", "subject": "p1", "predicate": "妹妹", "object_value": "林月", "object_entity": "p2", "evidence": evidence})
    return {"summary": text, "summary_evidence": evidence, "entities": entities,
            "events": [{"key": "e1", "title": event_title, "summary": text,
                        "participants": ["p1", "p2"], "mention_kind": "recollection" if recollection else "occurrence", "evidence": evidence}], "facts": facts}


@pytest.fixture
def raw_book(tmp_path, recorder):
    repo = Repository(Database(tmp_path / "book.db"))
    project = repo.create_project("时间测试")["id"]
    chapters = [
        (1, "林舟取得钥匙，林月是他的妹妹。", "取得钥匙", "p1", False),
        (5, "林舟把钥匙交给林月。", "交出钥匙", "p2", False),
        (10, "林月怀疑地窖藏着秘密。", "怀疑地窖", None, False),
        (20, "林舟回忆交接之前修复钥匙的事，地窖密码是青石。", "回忆修复", None, True),
    ]
    for number, text, title, holder, secret in chapters:
        payload = chapter_payload(text, title, holder=holder, secret=secret, recollection=number == 20)
        client = FixtureLLMClient(recorder, {"b0001": payload})
        IngestionPipeline(repo, client, model_identity="test-book").ingest(project, number, title, text)
    events = {e["chapter_no"]: e["id"] for e in repo.records(project, "event")}
    facts = repo.records(project, "fact")
    return {"repo": repo, "project": project, "events": events,
            "first": next(f["id"] for f in facts if f["chapter_no"] == 1 and f["predicate"] == "持有人"),
            "second": next(f["id"] for f in facts if f["chapter_no"] == 5),
            "secret": next(f["id"] for f in facts if f["predicate"] == "密码"),
            "graph": TimelineService(repo), "knowledge": KnowledgeService(repo)}


@pytest.fixture
def book(raw_book):
    b = raw_book
    graph, e, k = b["graph"], b["events"], b["knowledge"]
    for chapter, tick in ((1, 0), (20, 1), (5, 2), (10, 3)):
        graph.set_time(e[chapter], start=tick, revealed_at_chapter=chapter, reason="作者标注")
    graph.link(e[1], e[20], "before", revealed_at_chapter=20, reason="回忆在交接之前")
    graph.link(e[20], e[5], "before", revealed_at_chapter=20, reason="回忆顺序")
    graph.link(e[1], e[5], "before", revealed_at_chapter=5, reason="已知交接顺序")
    k.bind_fact(b["first"], from_event=e[1], until_event=e[5], reason="首次持有区间")
    k.bind_fact(b["second"], from_event=e[5], reason="交接后状态")
    k.bind_fact(b["secret"], from_event=e[1], mode="assertion", reason="密码是早已存在的设定")
    return b


def test_v1_upgrade_preserves_data_and_keeps_backup(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(files("easy_writing.storage").joinpath("schema.sql").read_text(encoding="utf-8"))
    conn.execute("INSERT INTO projects VALUES('old','旧小说','before')")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()
    db = Database(path)
    assert Repository(db).projects()[0]["id"] == "old"
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION
    backup = path.with_name(path.name + ".pre-v2.bak")
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM projects").fetchone()[0] == "old"


def test_story_and_narrative_orders_differ_without_invented_dates(book):
    g, p, e = book["graph"], book["project"], book["events"]
    story = g.timeline(p)
    assert [group[0] for group in story["unordered_groups"]] == [e[1], e[20], e[5], e[10]]
    narrative = g.timeline(p, order="narrative")
    assert [m["chapter_no"] for m in narrative["mentions"]] == [1, 5, 10, 20]
    assert narrative["mentions"][-1]["mention_kind"] == "recollection"
    early = g.timeline(p, through_chapter=10)
    assert e[20] not in {event["id"] for event in early["events"]}


def test_unknown_time_is_partial_order(raw_book):
    result = raw_book["graph"].timeline(raw_book["project"])
    assert len(result["unordered_groups"]) == 1
    assert len(result["time_unknown"]) == 4
    assert result["partial_order"]


def test_strict_cycle_retained_as_candidate_not_confirmed(book):
    result = book["graph"].link(book["events"][5], book["events"][1], "before", reason="错误的回路")
    assert result["record"]["review_status"] == "needs_review"
    assert result["validation_errors"]
    assert book["graph"].timeline(book["project"])["unordered_groups"][0] == [book["events"][1]]


def test_known_time_conflict_and_rollback(book):
    g, e, p = book["graph"], book["events"], book["project"]
    with pytest.raises(StorageError, match="冲突"):
        g.set_time(e[1], start=9, reason="错误时间")
    event = next(row for row in g.timeline(p)["events"] if row["id"] == e[1])
    assert event["time"]["start_tick"] == 0


def test_transitive_time_conflict_through_unknown_node(book):
    g, e = book["graph"], book["events"]
    g.set_time(e[10], reason="清除时间")
    assert g.link(e[5], e[10], "before", reason="连接未知时间")["record"]["review_status"] == "confirmed"
    result = g.link(e[10], e[20], "before", reason="冲突的传递顺序")
    assert result["record"]["review_status"] == "needs_review"


@pytest.mark.parametrize("relation", ["during", "overlaps"])
def test_interval_conflicts_are_not_confirmed(book, relation):
    result = book["graph"].link(book["events"][1], book["events"][5], relation, reason="不匹配的区间")
    assert result["record"]["review_status"] == "needs_review"


def test_valid_interval_and_frame_checks(raw_book):
    g, e = raw_book["graph"], raw_book["events"]
    g.set_time(e[1], start=0, end=10, reason="区间")
    g.set_time(e[5], start=2, end=3, reason="区间")
    assert g.link(e[5], e[1], "during", reason="包含")["record"]["review_status"] == "confirmed"
    assert g.link(e[1], e[5], "overlaps", reason="重叠")["record"]["review_status"] == "confirmed"
    g.set_time(e[20], start=20, frame="dream", reason="梦境时间")
    result = g.link(e[5], e[20], "before", reason="不可比较的参照系")
    assert result["record"]["review_status"] == "needs_review"


def test_causality_does_not_follow_from_before_and_non_temporal_cycles_allowed(book):
    g, e = book["graph"], book["events"]
    assert {l["relation_type"] for l in g.timeline(book["project"])["links"]} == {"before"}
    assert g.link(e[1], e[5], "causes", reason="作者因果")["record"]["review_status"] == "confirmed"
    assert g.link(e[5], e[1], "causes", reason="允许非时间环")["record"]["review_status"] == "confirmed"


def test_multiple_predecessors_neighbors_and_budget(book):
    g, e, p = book["graph"], book["events"], book["project"]
    incoming = g.neighbors(p, e[5], direction="in", relation="before")
    assert {item["event"]["id"] for item in incoming["events"]} == {e[1], e[20]}
    result = g.neighbors(p, e[1], direction="out", relation="before", max_nodes=1)
    assert result["truncated"] and result["events"] == []


def test_author_only_annotations_do_not_enter_reader_view(raw_book):
    g, e, p = raw_book["graph"], raw_book["events"], raw_book["project"]
    g.set_time(e[1], start=0, reason="只给作者")
    g.link(e[1], e[5], "before", reason="只给作者")
    assert g.timeline(p)["links"]
    reader = g.timeline(p, through_chapter=10)
    assert reader["links"] == [] and e[1] in reader["time_unknown"]


def test_demoting_core_preserves_other_plot_evidence_and_paths(book):
    g, e, p, repo = book["graph"], book["events"], book["project"], book["repo"]
    main_plot = g.create_plot(p, "主线")["id"]
    sub_plot = g.create_plot(p, "支线")["id"]
    for event in (e[1], e[20], e[5]):
        g.set_role(main_plot, event, "core", reason="骨架")
    g.set_role(sub_plot, e[20], "core", reason="支线核心")
    before = g.plot_view(main_plot)
    with repo.db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
    evidence = repo.evidence("event", e[20])
    g.set_role(main_plot, e[20], "ordinary", reason="降级")
    after = g.plot_view(main_plot)
    assert len(after["events"]) == 2
    assert any(path["via"] == [e[20]] and path["relation_types"] == ["before", "before"] for path in after["paths"])
    assert after["snapshot_id"] != before["snapshot_id"]
    assert g.plot_view(sub_plot)["events"][0]["role"] == "core"
    assert len(g.plot_view(main_plot, expanded=True)["events"]) == 3
    assert repo.evidence("event", e[20]) == evidence
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0] == count
    assert e[20] in {row["id"] for row in repo.records(p, "event")}


def test_overview_does_not_invent_missing_path_and_honors_budget(book):
    g, e, p = book["graph"], book["events"], book["project"]
    plot = g.create_plot(p, "孤立线")["id"]
    g.set_role(plot, e[1], "core", reason="起点")
    g.set_role(plot, e[10], "core", reason="孤立节点")
    assert g.plot_view(plot)["paths"] == []
    g.set_role(plot, e[5], "core", reason="终点")
    g.set_role(plot, e[20], "ordinary", reason="中间")
    assert g.plot_view(plot, max_paths=1)["truncated"]


def test_duplicate_event_alignment_preserves_mentions_and_sources(book, recorder):
    repo, g, p, e = book["repo"], book["graph"], book["project"], book["events"]
    text = "林月回忆林舟交出钥匙的往事。"
    response = chapter_payload(text, "复述交接", recollection=True)
    IngestionPipeline(repo, FixtureLLMClient(recorder, {"b0001": response}), model_identity="repeat").ingest(p, 25, "复述", text)
    duplicate = next(row["id"] for row in repo.records(p, "event") if row["chapter_no"] == 25)
    g.merge_events(duplicate, e[5], reason="同一交接事件", revealed_at_chapter=25)
    narrative = g.timeline(p, order="narrative")["mentions"]
    assert sum(m["event_id"] == e[5] for m in narrative) == 2
    assert len(repo.evidence("event", e[5])) == 2
    assert len(g.timeline(p)["events"]) == 4


def test_alignment_cannot_create_before_self(raw_book):
    g, e = raw_book["graph"], raw_book["events"]
    g.link(e[1], e[5], "before", reason="明确的不同事件")
    with pytest.raises(StorageError, match="冲突"):
        g.merge_events(e[1], e[5], reason="错误对齐")
    with raw_book["repo"].db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_identity").fetchone()[0] == 0


def test_holder_before_and_after_transfer(book):
    k, p, e = book["knowledge"], book["project"], book["events"]
    before = k.facts(p, subject="钥匙", predicate="持有人", at_event=e[1])["facts"]
    after = k.facts(p, subject="钥匙", predicate="持有人", at_event=e[5])["facts"]
    assert [f["object_name"] for f in before] == ["林舟"]
    assert [f["object_name"] for f in after] == ["林月"]


def test_single_value_state_overwrites_only_when_order_is_known(book):
    k, p, e = book["knowledge"], book["project"], book["events"]
    k.bind_fact(book["first"], from_event=e[1], reason="没有明确结束节点")
    result = k.facts(p, subject="钥匙", predicate="持有人", at_event=e[5])["facts"]
    assert [f["object_name"] for f in result] == ["林月"]


def test_unknown_start_is_marked_uncertain_not_guessed(raw_book):
    k, p, e = raw_book["knowledge"], raw_book["project"], raw_book["events"]
    result = k.facts(p, subject="钥匙", at_event=e[10])
    assert result["facts"] and all(f["time_uncertain"] for f in result["facts"])


def test_future_revelation_never_enters_chapter_ten_query(book):
    k, p = book["knowledge"], book["project"]
    result = k.facts(p, subject="地窖", through_chapter=10)
    assert result["facts"] == []
    assert "青石" not in json.dumps(k.answer(p, "第十章地窖的密码是什么？"), ensure_ascii=False)
    assert k.facts(p, subject="地窖", through_chapter=20)["facts"][0]["object_name"] == "青石"


def test_reader_knowledge_not_automatically_character_knowledge(book):
    k, p, e = book["knowledge"], book["project"], book["events"]
    assert k.facts(p, subject="地窖")["facts"]
    assert k.facts(p, actor="林月", subject="地窖", at_event=e[10])["facts"] == []
    k.learn(book["secret"], "林月", e[10], reason="获知密码", revealed_at_chapter=20)
    assert k.facts(p, actor="林月", subject="地窖", at_event=e[5])["facts"] == []
    assert k.facts(p, actor="林月", subject="地窖", at_event=e[10])["facts"]
    assert k.facts(p, actor="林月", subject="地窖", through_chapter=10)["facts"] == []


def test_awareness_visibility_and_natural_actor_routing(book):
    k, p, e = book["knowledge"], book["project"], book["events"]
    k.learn(book["second"], "林月", e[5], reason="她亲自收下钥匙", revealed_at_chapter=5)
    result = k.answer(p, "第五章林月知道钥匙的持有人吗？", at_event=e[5])
    assert result["facts"] and result["facts"][0]["object_name"] == "林月"
    assert result["model_calls"] == 0
    with pytest.raises(StorageError, match="actor"):
        k.answer(p, "主角知道地窖的密码吗？")


def test_people_relations_history_coverage_and_no_model_calls(book, recorder):
    k, p = book["knowledge"], book["project"]
    before = len(recorder.events)
    assert {c["name"] for c in k.characters(p)} == {"林舟", "林月"}
    assert k.relations(p)["relations"][0]["predicate"] == "妹妹"
    answer = k.answer(p, "钥匙由谁持有？", at_event=book["events"][5])
    assert "林月" in answer["answer"]
    assert len(recorder.events) == before
    assert answer["coverage"]["missing_chapter_ranges"]


def test_actor_or_event_from_different_project_is_rejected(book):
    repo, g = book["repo"], book["graph"]
    other = repo.create_project("另一本小说")["id"]
    with pytest.raises(StorageError):
        g.neighbors(other, book["events"][1])
    with pytest.raises(StorageError):
        g.set_role(g.create_plot(other, "别的主线")["id"], book["events"][1], "core", reason="越界")


@pytest.mark.parametrize("question,number", [("第十章", 10), ("第二十章", 20), ("第十二章", 12), ("第102章", 102), ("第一百零二章", 102)])
def test_chinese_chapter_parser(question, number):
    assert KnowledgeService._chapter_number(question) == number


def test_cli_read_and_edit_commands(book, capsys):
    db, p, e = str(book["repo"].db.path), book["project"], book["events"]
    def call(args):
        assert main([*args, "--db", db]) == 0
        return json.loads(capsys.readouterr().out)
    assert call(["characters", "--project", p])
    assert call(["timeline", "--project", p, "--order", "narrative"])["mentions"]
    assert call(["state", "--project", p, "--subject", "钥匙", "--at-event", e[5]])["facts"]
    assert call(["query", "--project", p, "--question", "第十章地窖密码是什么？"])["facts"] == []
    plot = call(["plot", "create", "--project", p, "--name", "CLI主线"])["id"]
    call(["plot", "role", "--plot", plot, "--event", e[1], "--role", "core", "--reason", "设为核心"])
    assert call(["plot", "view", "--plot", plot])["events"][0]["role"] == "core"


def test_hidden_relation_between_visible_events_is_not_revealed(book):
    g, e, p = book["graph"], book["events"], book["project"]
    g.link(e[1], e[5], "causes", revealed_at_chapter=20, reason="第二十章才解释的因果")
    assert g.neighbors(p, e[1], relation="causes")["events"]
    assert g.neighbors(p, e[1], relation="causes", through_chapter=10)["events"] == []


def test_hidden_alignment_does_not_leak_copied_time(raw_book):
    g, e, p = raw_book["graph"], raw_book["events"], raw_book["project"]
    g.set_time(e[1], start=0, revealed_at_chapter=1, reason="第一章已知时间")
    g.merge_events(e[1], e[5], reason="第二十章揭示两处提及相同", revealed_at_chapter=20)
    reader = g.timeline(p, through_chapter=10)
    assert len(reader["events"]) == 3
    target = next(item for item in reader["events"] if item["id"] == e[5])
    assert target["time"] is None
    assert len(g.timeline(p)["events"]) == 3


def test_rejecting_canonical_event_hides_merged_mentions(raw_book):
    b = raw_book
    b["graph"].merge_events(b["events"][1], b["events"][5], reason="身份对齐")
    b["repo"].edit("event", b["events"][5], status="rejected", reason="作者否决该事件")
    assert len(b["graph"].timeline(b["project"])["events"]) == 2


def test_multiple_chapter_question_requires_explicit_separate_queries(book):
    with pytest.raises(StorageError, match="单一章节"):
        book["knowledge"].answer(book["project"], "第一章和第二十章钥匙分别归谁？")


def test_candidate_does_not_silently_replace_author_confirmed_state(book):
    b = book
    b["knowledge"].bind_fact(b["first"], from_event=b["events"][1], reason="没有确认终点")
    b["repo"].edit("fact", b["first"], status="confirmed", reason="作者确认")
    result = b["knowledge"].facts(b["project"], subject="钥匙", predicate="持有人", at_event=b["events"][5])
    assert len(result["facts"]) == 2


def test_reader_fact_does_not_expose_future_end_event_id(book):
    result = book["knowledge"].facts(book["project"], subject="钥匙", through_chapter=1, at_event=book["events"][1])
    assert book["events"][5] not in json.dumps(result)
    assert result["facts"][0]["effective_until"] is None


def test_demo_script_is_repeatable_and_never_uses_real_api(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "scripts/demo_stage3.py"), "--db", str(tmp_path / "demo.db")]
    for attempt in range(2):
        result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True)
        report = json.loads(result.stdout)
        assert report["real_model_calls"] == 0
        assert report["holder_before"] == "林舟" and report["holder_after"] == "林月"
        assert report["reader_chapter10_secret_hidden"]
        assert report["cached_chapters"] == (5 if attempt else 0)


def test_cli_graph_mutations_and_link_review(raw_book, capsys):
    db, e, p = str(raw_book["repo"].db.path), raw_book["events"], raw_book["project"]
    def call(args):
        assert main([*args, "--db", db]) == 0
        return json.loads(capsys.readouterr().out)
    call(["event-time", "--event", e[1], "--start", "0", "--revealed-at", "1", "--reason", "时间"])
    link = call(["event-link", "--source", e[1], "--target", e[5], "--relation", "before", "--status", "unconfirmed", "--reason", "候选"])["record"]
    reviewed = call(["edit", "--kind", "link", "--id", link["id"], "--status", "confirmed", "--reason", "确认"])
    assert reviewed["record"]["review_status"] == "confirmed"
    call(["fact-context", "--id", raw_book["second"], "--from-event", e[5], "--reason", "区间"])
    call(["learn", "--fact", raw_book["second"], "--actor", "林月", "--event", e[5], "--revealed-at", "5", "--reason", "获知"])
    assert call(["event-neighbors", "--project", p, "--event", e[1], "--direction", "out"])["events"]


def test_before_chapter_boundary_is_exclusive():
    assert KnowledgeService._chapter_number("第十章之前知道什么") == 9


@pytest.mark.parametrize("question", ["第一章之前知道什么", "第一万章时知道什么"])
def test_unrecognized_or_empty_scope_never_falls_back_to_whole_book(question):
    with pytest.raises(StorageError):
        KnowledgeService._chapter_number(question)


def test_failed_migration_is_atomic(tmp_path):
    path = tmp_path / "collision.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(files("easy_writing.storage").joinpath("schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO projects VALUES('old','保留数据','before')")
        conn.execute("CREATE TABLE plot_threads(custom_column TEXT)")
        conn.execute("PRAGMA user_version=1")
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT id FROM projects").fetchone()[0] == "old"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='event_times'").fetchone() is None


def test_clearing_time_allows_author_to_resolve_identity_conflict(raw_book):
    b, e = raw_book, raw_book["events"]
    b["graph"].set_time(e[1], start=0, reason="旧时间")
    b["graph"].set_time(e[5], start=2, reason="核对后的时间")
    with pytest.raises(StorageError, match="不同"):
        b["graph"].merge_events(e[1], e[5], reason="先发现冲突")
    assert b["graph"].set_time(e[1], reason="撤销旧标注")["cleared"]
    result = b["graph"].merge_events(e[1], e[5], reason="现在确认同一事件")
    assert result["target_id"] == e[5]
