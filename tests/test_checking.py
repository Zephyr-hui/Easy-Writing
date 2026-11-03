import json
import sqlite3
from importlib.resources import files
from types import SimpleNamespace

import pytest

from easy_writing.cli import main
from easy_writing.desktop.presentation import report_sections
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.checking import CheckingService
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid
from easy_writing.retrieval.corpus import CorpusBuilder


def payload(text, title, facts=(), threads=(), recollection=False):
    evidence = [{"paragraph_id": "p0001", "quote": text}]
    entities = [{"key": key, "name": name, "kind": kind, "evidence": evidence} for key, name, kind in (
        ("zhou", "林舟", "person"), ("yue", "林月", "person"), ("key", "钥匙", "item"),
        ("room", "地窖", "place"), ("door", "铁门", "item"), ("rule", "世界规则", "concept"))]
    return {"summary": text, "summary_evidence": evidence, "entities": entities,
        "events": [{"key": "event", "title": title, "summary": text, "evidence": evidence,
                    "mention_kind": "recollection" if recollection else "occurrence"}],
        "facts": [{"key": f"f{i}", "subject": f[0], "predicate": f[1], "object_value": f[2],
                   **({"object_entity": f[3]} if len(f) > 3 else {}), "effective_from": "event", "evidence": evidence} for i, f in enumerate(facts)],
        "foreshadowing": [{"key": f"t{i}", "title": t, "description": t, "evidence": evidence} for i, t in enumerate(threads)]}


@pytest.fixture
def review_book(tmp_path, recorder):
    repo = Repository(Database(tmp_path / "checks.db"))
    project = repo.create_project("检查样稿")["id"]
    chapters = [
        (1, "林舟取得钥匙。地窖密码是青石。此世铁门不得穿墙。地上留有划痕和雾气。", "取得钥匙", [("key", "持有人", "林舟", "zhou"), ("room", "密码", "青石"), ("rule", "通行限制", "铁门不得穿墙")], ["划痕", "雾气"], False),
        (2, "林舟把钥匙交给林月。", "正常交接", [("key", "持有人", "林月", "yue")], [], False),
        (3, "林月仍持有钥匙，林舟也持有同一把钥匙；林月输入青石打开地窖，林舟穿墙通过铁门。", "冲突与使用密码", [("key", "持有人", "林舟", "zhou"), ("door", "穿墙", "成功")], [], False),
        (4, "林舟这才第一次告诉林月，地窖密码是青石。", "获知密码", [], [], False),
        (5, "林月销毁了钥匙，划痕原来是藏宝图的刻线。", "销毁与解释", [], [], False),
        (6, "林舟回忆交接之前修复钥匙的往事。", "倒叙修复", [], [], True),
    ]
    for no, text, title, facts, threads, recollection in chapters:
        data = payload(text, title, facts, threads, recollection)
        IngestionPipeline(repo, FixtureLLMClient(recorder, {"b0001": data}), model_identity="checks-fixture").ingest(project, no, title, text)
    for kind in ("event", "fact"):
        for row in repo.records(project, kind):
            repo.edit(kind, row["id"], status="confirmed", reason="样稿人工标注")
    events = {r["chapter_no"]: r["id"] for r in repo.records(project, "event")}
    facts = repo.records(project, "fact")
    holders = {r["chapter_no"]: r["id"] for r in facts if r["predicate"] == "持有人"}
    secret = next(r["id"] for r in facts if r["predicate"] == "密码")
    rule = next(r["id"] for r in facts if r["predicate"] == "通行限制")
    violation = next(r["id"] for r in facts if r["predicate"] == "穿墙")
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    for number, tick in ((1, 0), (6, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
        graph.set_time(events[number], start=tick, revealed_at_chapter=number, reason="故事刻度")
    knowledge.bind_fact(holders[1], from_event=events[1], until_event=events[2], reason="正常转交")
    knowledge.bind_fact(holders[2], from_event=events[2], until_event=events[5], reason="明确仍持有直至销毁")
    knowledge.bind_fact(holders[3], from_event=events[3], reason="另一处持有叙述")
    knowledge.learn(secret, "林月", events[4], revealed_at_chapter=4, reason="第一次获知")
    checks, threads = CheckingService(repo), ForeshadowingService(repo)
    use = checks.record_use(project, secret, "林月", events[3], reason="原文明示输入密码")
    constraint = checks.add_rule(project, rule, "铁门", "穿墙", "forbid", "成功", reason="作者明确的无条件规则")
    thread = next(t for t in threads.list(project)["threads"] if t["title"] == "划痕")["id"]
    fog = next(t for t in threads.list(project)["threads"] if t["title"] == "雾气")["id"]
    threads.transition(project, thread, "planted", event_id=events[1], reason="确认伏笔")
    threads.transition(project, fog, "open", reason="作者保留氛围与开放解释")
    graph.link(events[5], events[1], "resolves", reason="后文解释划痕", revealed_at_chapter=5)
    return dict(repo=repo, project=project, events=events, holders=holders, secret=secret, rule=rule,
        violation=violation, graph=graph, knowledge=knowledge, checks=checks, threads=threads,
        thread=thread, fog=fog, use=use, constraint=constraint, recorder=recorder)


def test_detects_real_conflicts_with_both_sources_and_does_not_change_knowledge(review_book):
    b = review_book
    before = b["threads"].list(b["project"])
    report = b["checks"].run(b["project"])
    by_kind = {f["kind"]: f for f in report["findings"]}
    assert {"state_conflict", "early_knowledge", "world_rule", "thread_suggestion"} == set(by_kind)
    for kind in ("state_conflict", "early_knowledge", "world_rule"):
        finding = by_kind[kind]
        assert finding["certainty"] == "confirmed_conflict"
        assert all(side["evidence"] for side in finding["evidence_sides"])
        assert finding["conditions"] and finding["repair_suggestions"]
        for side in finding["evidence_sides"]:
            assert all(e["version_id"] and e["paragraph_id"] and e["quote"] for e in side["evidence"])
    assert by_kind["state_conflict"]["record_keys"] == sorted([f"fact:{b['holders'][2]}", f"fact:{b['holders'][3]}"])
    assert by_kind["thread_suggestion"]["proposed_state"] == "partially_resolved"
    assert b["threads"].list(b["project"])["threads"] == before["threads"]
    assert report["real_model_calls"] == 0 and not report["automatic_changes_applied"]
    assert not report["stale"]


def test_normal_state_replacement_and_flashback_are_not_errors(review_book):
    b = review_book
    # 去掉显式结束，作者声明的更晚状态正常替换先前状态。
    b["knowledge"].bind_fact(b["holders"][2], from_event=b["events"][2], reason="后续转交")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] in ("state_conflict", "timeline_conflict", "temporal_candidate") for f in report["findings"])
    assert report["examined"]["events"] == 6


def test_report_text_and_model_advice_keep_findings_separate(review_book):
    b = review_book
    service, project = b["checks"], b["project"]
    report = service.run(project)
    text = "\n".join("\n".join(section) for section in report_sections(report))
    assert "已确认冲突" in text and "第 3 章" in text and "划痕" in text
    assert "snapshot_id" not in text and report["id"] not in text and "partially_resolved" not in text

    class Model:
        def extract_json(self, prompt, schema, *, system_prompt):
            packet = json.loads(prompt)
            assert packet["finding_sample"][0]["certainty"] == "confirmed_conflict"
            assert any("钥匙" in ref["quote"] for item in packet["finding_sample"] for ref in item["evidence"])
            assert "snapshot_id" not in packet and "禁止编造冲突" in system_prompt
            return SimpleNamespace(content=schema(overview="建议先核对角色持有物品的顺序。", suggestions=["检查钥匙的交接。"]))

    advice = service.suggest(project, report["id"], Model())
    reloaded = service.report(project, report["id"])
    assert reloaded["findings"] == report["findings"] and reloaded["model_advice"] == advice
    assert ("模型建议 · 供作者参考", "建议先核对角色持有物品的顺序。\n\n• 检查钥匙的交接。") in report_sections(reloaded)
    b["repo"].edit("event", b["events"][1], status="confirmed", reason="重新核对来源")
    with pytest.raises(StorageError, match="报告已过期"):
        service.suggest(project, report["id"], Model())


def test_unconfirmed_or_unknown_time_is_only_a_question(review_book):
    b = review_book
    b["repo"].edit("fact", b["holders"][3], status="unconfirmed", reason="未核实")
    report = b["checks"].run(b["project"])
    assert next(f for f in report["findings"] if f["kind"] == "state_conflict")["certainty"] == "needs_review"
    b["graph"].set_time(b["events"][3], reason="时间未定")
    report = b["checks"].run(b["project"])
    assert next(f for f in report["findings"] if f["kind"] == "state_conflict")["certainty"] == "needs_review"
    assert next(f for f in report["findings"] if f["kind"] == "awareness_gap")["certainty"] == "needs_review"


def test_missing_learning_is_not_proof_of_ignorance_and_earlier_channel_repairs(review_book):
    b = review_book
    with b["repo"].db.transaction() as conn:
        conn.execute("DELETE FROM fact_awareness WHERE fact_id=?", (b["secret"],))
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] == "early_knowledge" for f in report["findings"])
    gap = next(f for f in report["findings"] if f["kind"] == "awareness_gap")
    assert gap["certainty"] == "needs_review"
    b["knowledge"].learn(b["secret"], "林月", b["events"][1], reason="补记更早渠道")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] in ("early_knowledge", "awareness_gap") for f in report["findings"])


def test_temporal_candidate_is_reported_without_polluting_confirmed_graph(review_book):
    b = review_book
    bad = b["graph"].link(b["events"][5], b["events"][1], "before", reason="提取出的反向连接")["record"]
    assert bad["review_status"] == "needs_review"
    report = b["checks"].run(b["project"])
    finding = next(f for f in report["findings"] if f["kind"] == "temporal_candidate")
    assert finding["certainty"] == "needs_review"
    assert finding["record_keys"] == [f"link:{bad['id']}"]
    assert not report["temporal_dependent_checks_skipped"]


def test_lifecycle_requires_explicit_confirmation_and_keeps_history(review_book):
    b = review_book
    transitions = ("reinforced", "partially_resolved", "resolved")
    for state in transitions:
        b["threads"].transition(b["project"], b["thread"], state, event_id=b["events"][5], reason=f"作者确认 {state}")
    with pytest.raises(StorageError, match="correction"):
        b["threads"].transition(b["project"], b["thread"], "candidate", reason="误点")
    b["threads"].transition(b["project"], b["thread"], "candidate", correction=True, reason="作者撤回回收判断")
    thread = next(t for t in b["threads"].list(b["project"])["threads"] if t["id"] == b["thread"])
    assert thread["state"] == "candidate" and len(thread["transitions"]) == 5
    assert len(b["repo"].history(b["project"], record_id=b["thread"])) == 5


def test_open_cancelled_resolved_threads_are_not_unfilled_errors(review_book):
    b = review_book
    b["threads"].transition(b["project"], b["thread"], "resolved", event_id=b["events"][5], reason="完整解释")
    cancelled = b["threads"].create(b["project"], "白鸟", "舍弃的线索", event_id=b["events"][1], reason="补录")
    b["threads"].transition(b["project"], cancelled["id"], "cancelled", reason="作者取消")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"].startswith("thread_") for f in report["findings"])
    assert not report["unresolved_threads_are_errors"]


def test_chapter_boundary_does_not_reveal_later_conflicts_or_payoff_state(review_book):
    b = review_book
    b["threads"].transition(b["project"], b["thread"], "resolved", event_id=b["events"][5], reason="第五章才回收")
    report = b["checks"].run(b["project"], through_chapter=2)
    assert report["finding_total"] == 0 and report["examined"]["events"] == 2
    thread = next(t for t in b["threads"].list(b["project"], through_chapter=2)["threads"] if t["id"] == b["thread"])
    assert thread["state"] == "planted" and thread["stored_state"] is None
    corpus = CorpusBuilder(b["repo"]).build(b["project"], through_chapter=2)
    record = next(r for r in corpus.records if r.key == f"thread:{b['thread']}")
    assert record.extra["state"] == "planted" and "resolved" not in record.text


def test_all_threads_are_scanned_beyond_paging_and_all_findings_pageable(review_book):
    b = review_book
    with b["repo"].db.transaction() as conn:
        generation = conn.execute("SELECT generation_id FROM threads WHERE id=?", (b["thread"],)).fetchone()[0]
        evidence = conn.execute("SELECT * FROM evidence_links WHERE record_type='thread' AND record_id=?", (b["thread"],)).fetchone()
        for i in range(123):
            tid = uid()
            conn.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (tid, generation, f"候选{i:03}", "人工盘点", "planted" if i < 105 else "candidate", "confirmed"))
            conn.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?)", (uid(), "thread", tid, evidence["source_chunk_id"], evidence["quote"], evidence["quote_start"], evidence["quote_end"]))
            if i < 105:
                conn.execute("INSERT INTO thread_transitions(id,thread_id,from_state,to_state,event_id,reason,created_at) VALUES(?,?,'candidate','planted',?,'分页测试','fixture')", (uid(), tid, b["events"][1]))
    seen, offset = [], 0
    while True:
        page = b["threads"].list(b["project"], limit=37, offset=offset)
        seen.extend(t["id"] for t in page["threads"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 125
    report = b["checks"].run(b["project"])
    assert report["examined"]["threads"] == 125 and report["all_current_records_scanned"]
    assert report["thread_state_counts"]["candidate"] == 18 and not report["retrieval_top_k_used"]
    assert report["finding_total"] == 109 and len(report["findings"]) == 100 and report["next_offset"] == 100
    seen, offset = [], 0
    while True:
        page = b["checks"].report(b["project"], report["id"], limit=17, offset=offset)
        seen.extend(f["id"] for f in page["findings"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == report["finding_total"]


def test_author_intent_survives_rerun_and_can_be_reopened(review_book):
    b = review_book
    first = b["checks"].run(b["project"])
    finding = next(f for f in first["findings"] if f["kind"] == "early_knowledge")
    b["checks"].decide(b["project"], finding["id"], "intentional", reason="角色假装首次获知，之后揭示")
    second = b["checks"].run(b["project"])
    item = next(f for f in second["findings"] if f["kind"] == "early_knowledge")
    assert item["status"] == "intentional" and item["effective_severity"] == "info"
    assert not b["checks"].report(b["project"], first["id"])["stale"]
    b["checks"].decide(b["project"], item["id"], "open", reason="作者撤销误导安排")
    assert b["checks"].decision_history(b["project"])["total"] == 2
    assert b["checks"].report(b["project"], second["id"], status="intentional")["filtered_total"] == 0


def test_changed_evidence_invalidates_report_and_does_not_inherit_ignored_decision(review_book):
    b = review_book
    first = b["checks"].run(b["project"])
    f = next(f for f in first["findings"] if f["kind"] == "state_conflict")
    b["checks"].decide(b["project"], f["id"], "ignored", reason="暂缓")
    b["repo"].edit("fact", b["holders"][3], field="object_value", value="陌生人", reason="修正身份")
    assert b["checks"].report(b["project"], first["id"])["stale"]
    with pytest.raises(StorageError, match="过期"):
        b["checks"].decide(b["project"], f["id"], "resolved", reason="旧报告不可处理")
    second = b["checks"].run(b["project"])
    updated = next(f for f in second["findings"] if f["kind"] == "state_conflict")
    assert updated["status"] == "open" and updated["fingerprint"] != f["fingerprint"]


def test_disabled_annotations_stop_checks_and_rule_source_must_be_confirmed(review_book):
    b = review_book
    b["checks"].enable_annotation(b["project"], "rule", b["constraint"]["id"], False, reason="存在例外")
    b["checks"].enable_annotation(b["project"], "use", b["use"]["id"], False, reason="该行动并未使用密码")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] in ("world_rule", "early_knowledge", "awareness_gap") for f in report["findings"])
    b["repo"].edit("fact", b["rule"], status="unconfirmed", reason="规则尚不确定")
    with pytest.raises(StorageError, match="已确认"):
        b["checks"].add_rule(b["project"], b["rule"], "铁门", "穿墙", "forbid", "成功", reason="不能用未确认规则")


def test_revised_chapter_cannot_feed_old_facts_uses_or_rule_evidence(review_book):
    b = review_book
    report = b["checks"].run(b["project"])
    text = "林月猜测钥匙已被盗走，她没有打开地窖。"
    IngestionPipeline(b["repo"], FixtureLLMClient(b["recorder"], {"b0001": payload(text, "修订后的行动")}), model_identity="checks-fixture").ingest(b["project"], 3, "修订", text)
    current = b["checks"].run(b["project"])
    assert not any(f["kind"] in ("state_conflict", "early_knowledge", "world_rule") for f in current["findings"])
    assert current["skipped_annotations"]["uses"] == 1
    assert b["checks"].report(b["project"], report["id"])["stale"]
    with pytest.raises(StorageError, match="当前"):
        b["checks"].record_use(b["project"], b["secret"], "林月", b["events"][3], reason="不能使用旧事件")


def test_cross_project_record_ids_are_rejected(review_book):
    b = review_book
    other = b["repo"].create_project("另一部作品")["id"]
    first = b["checks"].run(b["project"])
    with pytest.raises(StorageError):
        b["checks"].report(other, first["id"])
    with pytest.raises(StorageError):
        b["checks"].decide(other, first["findings"][0]["id"], "ignored", reason="不可跨作品")
    with pytest.raises(StorageError):
        b["threads"].transition(other, b["thread"], "resolved", reason="不可跨作品")
    with pytest.raises(StorageError):
        b["checks"].record_use(other, b["secret"], "林月", b["events"][3], reason="不可跨作品")


@pytest.mark.parametrize("limit,offset", [(0, 0), (501, 0), (1, -1)])
def test_invalid_paging_is_rejected(review_book, limit, offset):
    b = review_book
    for call in (b["threads"].list, b["checks"].runs, b["checks"].decision_history):
        with pytest.raises(StorageError, match="分页"):
            call(b["project"], limit=limit, offset=offset)


def test_cli_check_thread_and_decision_entries(review_book, capsys):
    b = review_book
    args = ["--project", b["project"], "--db", str(b["repo"].db.path)]
    assert main(["check", "run", *args]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["finding_total"] == 4
    assert main(["check", "decide", *args, "--finding", report["findings"][0]["id"], "--status", "ignored", "--reason", "作者暂缓"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ignored"
    assert main(["thread", "list", *args, "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["next_offset"] == 1
    assert main(["knowledge-use", "list", *args]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert main(["check-rule", "disable", *args, "--id", b["constraint"]["id"], "--reason", "例外"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] == 0


def test_v3_migration_keeps_database_and_pre_v4_backup(tmp_path):
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    for name in ("schema.sql", "migration_002.sql", "migration_003.sql"):
        conn.executescript(files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8"))
    conn.execute("INSERT INTO projects VALUES('book','迁移样稿','old')")
    conn.execute("PRAGMA user_version=3")
    conn.commit()
    conn.close()
    db = Database(path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("SELECT name FROM projects").fetchone()[0] == "迁移样稿"
    with sqlite3.connect(path.with_name(path.name + ".pre-v4.bak")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_stale_payoff_cannot_be_reused_until_author_corrects_it(review_book):
    b = review_book
    b["threads"].transition(b["project"], b["thread"], "resolved", event_id=b["events"][5], reason="旧稿回收")
    text = "林月仍在寻找划痕的含义，没有销毁钥匙。"
    IngestionPipeline(b["repo"], FixtureLLMClient(b["recorder"], {"b0001": payload(text, "新稿尚未解释")}), model_identity="checks-fixture").ingest(b["project"], 5, "修订", text)
    thread = next(t for t in b["threads"].list(b["project"])["threads"] if t["id"] == b["thread"])
    assert thread["state"] == "planted" and thread["state_needs_review"]
    report = b["checks"].run(b["project"])
    assert any(f["kind"] == "thread_stale_basis" for f in report["findings"])
    with pytest.raises(StorageError, match="过期"):
        b["threads"].transition(b["project"], b["thread"], "reinforced", reason="不能默默沿用")
    new_event = next(e["id"] for e in b["repo"].records(b["project"], "event") if e["chapter_no"] == 5)
    b["threads"].transition(b["project"], b["thread"], "reinforced", event_id=new_event, correction=True, reason="新稿继续强化，不再回收")
    thread = next(t for t in b["threads"].list(b["project"])["threads"] if t["id"] == b["thread"])
    assert thread["state"] == "reinforced" and not thread["state_needs_review"]
    corpus = CorpusBuilder(b["repo"]).build(b["project"])
    record = next(r for r in corpus.records if r.key == f"thread:{b['thread']}")
    assert record.extra["state"] == "reinforced" and "resolved" not in record.text


def test_bounded_rule_respects_points_and_persistent_state_overlap(review_book):
    b = review_book
    b["checks"].enable_annotation(b["project"], "rule", b["constraint"]["id"], False, reason="改为有生效区间的规则")
    b["checks"].add_rule(b["project"], b["rule"], "铁门", "穿墙", "forbid", "成功", from_event=b["events"][4], until_event=b["events"][5], reason="第四至第五章的规则")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] == "world_rule" for f in report["findings"])  # 第三章的单次事件不适用。
    b["knowledge"].bind_fact(b["violation"], from_event=b["events"][1], until_event=b["events"][5], mode="state", reason="标注持续的通行能力")
    report = b["checks"].run(b["project"])
    assert next(f for f in report["findings"] if f["kind"] == "world_rule")["certainty"] == "confirmed_conflict"
    b["knowledge"].bind_fact(b["violation"], from_event=b["events"][1], until_event=b["events"][4], mode="state", reason="能力在规则生效前终止")
    assert not any(f["kind"] == "world_rule" for f in b["checks"].run(b["project"])["findings"])


def test_source_rule_rejection_skips_constraint_instead_of_using_old_setting(review_book):
    b = review_book
    b["repo"].edit("fact", b["rule"], status="rejected", reason="原提取是角色传闻，不是世界规则")
    report = b["checks"].run(b["project"])
    assert not any(f["kind"] == "world_rule" for f in report["findings"])
    assert report["skipped_annotations"]["rules"] == 1


def test_claim_holder_and_same_named_text_are_not_confirmed_global_conflicts(review_book):
    b = review_book
    # 同一文字名与实体指向尚不构成两个不同对象的证明。
    b["repo"].edit("fact", b["holders"][3], field="object_value", value="林月", reason="改成未对齐的文字身份")
    f = next(f for f in b["checks"].run(b["project"])["findings"] if f["kind"] == "state_conflict")
    assert f["certainty"] == "needs_review"
    actor_id = next(e["id"] for e in b["repo"].records(b["project"], "entity") if e["name"] == "林舟")
    with b["repo"].db.transaction() as conn:
        conn.execute("UPDATE facts SET knowledge_holder_id=? WHERE id=?", (actor_id, b["holders"][3]))
    assert not any(f["kind"] == "state_conflict" for f in b["checks"].run(b["project"])["findings"])


def test_confirmed_broken_time_graph_gets_report_without_crashing(review_book):
    b = review_book
    # 模拟外部写库造成损坏；正常 TimelineService 不会确认这个环。
    bad = b["graph"].link(b["events"][5], b["events"][1], "before", reason="损坏候选")["record"]
    with b["repo"].db.transaction() as conn:
        conn.execute("UPDATE event_links SET review_status='confirmed' WHERE id=?", (bad["id"],))
    report = b["checks"].run(b["project"])
    assert report["temporal_dependent_checks_skipped"]
    assert any(f["kind"] == "timeline_conflict" for f in report["findings"])
    assert not any(f["kind"] in ("early_knowledge", "state_conflict", "world_rule") for f in report["findings"])


def test_resolves_direction_is_payoff_to_plant_and_foreshadows_is_forward(review_book):
    b = review_book
    link = next(l for l in b["graph"].timeline(b["project"])["links"] if l["relation_type"] == "resolves")
    assert link["source"] == b["events"][5] and link["target"] == b["events"][1]
    finding = next(f for f in b["checks"].run(b["project"])["findings"] if f["kind"] == "thread_suggestion")
    assert all(e["chapter_no"] == 5 for e in finding["evidence_sides"][1]["evidence"])
    b["graph"].review_link(link["id"], "rejected", "取消原关联以核对方向")
    b["graph"].link(b["events"][1], b["events"][5], "resolves", reason="故意反向，不能解释第一章的埋设")
    assert not any(f["kind"] == "thread_suggestion" for f in b["checks"].run(b["project"])["findings"])
    b["graph"].link(b["events"][1], b["events"][3], "foreshadows", reason="第一章为第三章铺垫")
    finding = next(f for f in b["checks"].run(b["project"])["findings"] if f["kind"] == "thread_suggestion")
    assert finding["proposed_state"] == "reinforced"
    assert all(e["chapter_no"] == 3 for e in finding["evidence_sides"][1]["evidence"])
