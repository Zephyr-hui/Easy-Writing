import json
import sqlite3
import time
from importlib.resources import files

import pytest

from easy_writing.cli import main
from easy_writing.desktop.presentation import revision_sections
from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.llm.base import LLMError
from easy_writing.retrieval.corpus import CorpusBuilder
from easy_writing.retrieval.embeddings import FakeEmbeddingProvider
from easy_writing.retrieval.hybrid import HybridRetriever
from easy_writing.services.archive import ArchiveService
from easy_writing.services.checking import CheckingService
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.recovery import RecoveryService
from easy_writing.services.revisions import RevisionService
from easy_writing.services.statistics import StatisticsService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database, StorageError
from easy_writing.storage.repository import Repository, uid


def fixture(secret="青石", *, distinct=False):
    first = "林舟取得钥匙，林月在门前发现月牙划痕。"
    second = f"旧宅地窖的密码是{secret}。"
    ev1 = [{"paragraph_id": "p0001", "quote": first}]
    ev2 = [{"paragraph_id": "p0002", "quote": second}]
    payload = {"summary": first + second, "summary_evidence": ev1 + ev2,
        "entities": [{"key": k, "name": n, "kind": kind, "evidence": ev1} for k,n,kind in (("zhou","林舟","person"),("yue","林月","person"),("key","钥匙","item"),("room","地窖","place"))],
        "events": [{"key": "arrival", "title": "取得钥匙", "summary": first, "participants": ["zhou","yue"], "evidence": ev1}],
        "facts": [{"key":"holder","subject":"key","predicate":"持有人","object_value":"林舟","object_entity":"zhou","effective_from":"arrival","evidence":ev1},
                  {"key":"secret","subject":"room","predicate":"密码","object_value":secret,"effective_from":"arrival","evidence":ev2}],
        "foreshadowing": [{"key":"crescent","title":"月牙划痕","description":"未解释的刻痕","evidence":ev1}]}
    return first + "\n\n" + second, payload


def ingest(repo, recorder, project, number=12, secret="青石"):
    text, response = fixture(secret)
    result = IngestionPipeline(repo, FixtureLLMClient(recorder, {"b0001": response}), model_identity="revision-fixture-v1").ingest(project, number, "旧宅", text)
    return result


@pytest.fixture
def revised_book(tmp_path, recorder):
    repo = Repository(Database(tmp_path / "maintenance.db"))
    project = repo.create_project("改稿验收")["id"]
    ingest(repo, recorder, project, 12)
    ingest(repo, recorder, project, 13)
    facts = repo.records(project, "fact")
    secret = {r["chapter_no"]:r["id"] for r in facts if r["predicate"] == "密码"}
    holder = next(r["id"] for r in facts if r["chapter_no"] == 12 and r["predicate"] == "持有人")
    events = {r["chapter_no"]:r["id"] for r in repo.records(project,"event")}
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    for no, event in events.items():
        graph.set_time(event, start=no, reason="作者标注顺序", revealed_at_chapter=no)
    for f in facts:
        repo.edit("fact", f["id"], status="confirmed", reason="作者确认")
    knowledge.bind_fact(holder, from_event=events[12], mode="state", reason="持有状态")
    knowledge.learn(secret[12], "林月", events[12], reason="旧密码获知")
    plot = graph.create_plot(project, "主线")["id"]
    graph.set_role(plot, events[12], "core", reason="关键节点")
    graph.set_role(plot, events[13], "core", reason="后续节点")
    graph.link(events[12], events[13], "before", reason="先后关系")
    thread = next(r["id"] for r in repo.records(project,"thread") if r["chapter_no"] == 12)
    ForeshadowingService(repo).transition(project, thread, "planted", event_id=events[12], reason="确认铺垫")
    revision = RevisionService(repo)
    revision.depend(project,"fact",secret[12],"fact",secret[13],reason="第十三章沿用第十二章的口令")
    return dict(repo=repo, project=project, recorder=recorder, secrets=secret, holder=holder, events=events, plot=plot, thread=thread, graph=graph, knowledge=knowledge, revision=revision)


def test_chapter12_rewrite_quarantines_dependencies_and_preserves_only_exact_annotations(revised_book):
    b = revised_book
    original = CheckingService(b["repo"]).run(b["project"])
    old_corpus = CorpusBuilder(b["repo"]).build(b["project"])
    provider = FakeEmbeddingProvider()
    HybridRetriever(b["repo"], provider).search(old_corpus, "地窖密码")
    ingest(b["repo"], b["recorder"], b["project"], 12, "白鹭")
    result = b["knowledge"].facts(b["project"], predicate="密码")
    assert [f["object_name"] for f in result["facts"]] == ["白鹭"]
    assert result["coverage"]["quarantined_chapters"] == [13]
    assert CheckingService(b["repo"]).report(b["project"],original["id"])["stale"]
    corpus = CorpusBuilder(b["repo"]).build(b["project"])
    assert all("青石" not in r.text for r in corpus.records)
    assert f"fact:{b['secrets'][12]}" not in {r.key for r in corpus.records}
    graph = b["graph"].timeline(b["project"])
    assert len(graph["events"]) == 1 and graph["events"][0]["time"]["start_tick"] == 12
    new_holder = next(r for r in b["repo"].records(b["project"],"fact") if r["chapter_no"] == 12 and r["predicate"] == "持有人")
    assert new_holder["review_status"] == "confirmed"
    new_thread = next(t for t in ForeshadowingService(b["repo"]).list(b["project"])["threads"] if t["chapter_no"] == 12)
    assert new_thread["state"] == "planted"
    assert b["graph"].plot_view(b["plot"])["events"][0]["role"] == "core"
    # 改变口令后不能沿用旧口令的获知标注。
    assert not b["knowledge"].facts(b["project"], actor="林月", predicate="密码")["facts"]


def test_transitive_dependency_reviews_require_explicit_author_resolution(revised_book):
    b = revised_book
    third = next(r["id"] for r in b["repo"].records(b["project"],"thread") if r["chapter_no"] == 13)
    b["revision"].depend(b["project"], "fact", b["secrets"][13], "thread", third, reason="刻痕解释依赖此口令")
    ingest(b["repo"], b["recorder"], b["project"], 12, "白鹭")
    status = b["revision"].status(b["project"], limit=500)
    targets = {(r["target_type"],r["target_id"]) for r in status["reviews"]}
    assert ("fact", b["secrets"][13]) in targets and ("thread",third) in targets
    described = b["revision"].status(b["project"], limit=500, describe=True)
    text = "\n".join("\n".join(section) for section in revision_sections(described))
    assert "变更来源　事实与关系 · 第 12 章 · 历史版本 1 · 地窖 · 密码 · 青石" in text
    assert "受影响记录　伏笔 · 第 13 章 · 月牙划痕" in text
    assert "第 13 章已暂时移出模型的知识查询范围" in text
    assert "target_id" not in text and b["secrets"][13] not in text
    other = b["repo"].create_project("其他作品")["id"]
    with pytest.raises(StorageError):
        b["revision"].resolve(other,status["reviews"][0]["id"],reason="不能跨项目")
    for review in status["reviews"]:
        b["revision"].resolve(b["project"],review["id"],reason="作者已对照当前稿复核；示例特意保留后文误导")
    assert not b["revision"].status(b["project"])["pending_total"]
    assert not b["knowledge"].facts(b["project"])["coverage"]["quarantined_chapters"]


def test_role_link_fact_edits_rebuild_views_and_vectors_without_losing_ordinary_node(revised_book):
    b = revised_book
    first = b["revision"].refresh(b["project"],provider=FakeEmbeddingProvider())
    assert first["vectors"]["new_segments"] > 0
    b["graph"].set_role(b["plot"],b["events"][12],"ordinary",reason="主线降级")
    assert all(v["stale"] for v in b["revision"].status(b["project"])["derived_views"])
    refreshed = b["revision"].refresh(b["project"],provider=FakeEmbeddingProvider())
    assert refreshed["vectors"]["new_segments"] == 0
    expanded = b["graph"].plot_view(b["plot"],expanded=True)
    assert next(e["role"] for e in expanded["events"] if e["id"] == b["events"][12]) == "ordinary"
    b["repo"].edit("event",b["events"][12],field="summary",value="作者修正取得钥匙的经过",reason="更正事件")
    linked = next(l for l in b["graph"].timeline(b["project"])["links"] if l["relation_type"] == "before")
    b["graph"].review_link(linked["id"],"rejected","取消顺序约束")
    result = b["revision"].refresh(b["project"],provider=FakeEmbeddingProvider())
    assert result["vectors"]["new_segments"] > 0
    assert not any(v["stale"] for v in b["revision"].status(b["project"])["derived_views"])
    assert not CheckingService(b["repo"]).report(b["project"],result["check_run_id"])["stale"]


def test_refresh_defers_summaries_of_quarantined_chapters(revised_book):
    b = revised_book
    ingest(b["repo"], b["recorder"], b["project"], 12, "白鹭")
    result = b["revision"].refresh(b["project"],provider=FakeEmbeddingProvider())
    assert 13 in result["deferred_summary_chapters"]
    assert all("青石" not in r.text for r in CorpusBuilder(b["repo"]).build(b["project"]).records)


def test_resume_reuses_completed_blocks_and_publishes_once(tmp_path, recorder):
    repo = Repository(Database(tmp_path/"resume.db"))
    project=repo.create_project("恢复")["id"]
    text="甲"*180+"\n\n"+"乙"*180
    first={"summary":"第一段", "summary_evidence":[{"paragraph_id":"p0001","quote":"甲"*20}]}
    second={"summary":"第二段", "summary_evidence":[{"paragraph_id":"p0002","quote":"乙"*20}]}
    client=FixtureLLMClient(recorder,{"b0001":first})
    pipeline=IngestionPipeline(repo,client,model_identity="resume-fixture",max_chars=200)
    with pytest.raises(LLMError): pipeline.ingest(project,1,"一",text)
    service=RecoveryService(repo)
    job=service.jobs(project)[0]
    assert job["completed_blocks"]==1 and job["status"]=="failed"
    resumed=FixtureLLMClient(recorder,{"b0002":second})
    result=service.resume(project,job["id"],resumed)
    assert result["model_calls"]==1
    assert service.resume(project,job["id"],resumed)["cached"]
    with repo.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM extraction_blocks").fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM ingestion_attempts").fetchone()[0]==2


def test_resume_rejects_active_lease_and_superseded_source(tmp_path,recorder):
    repo=Repository(Database(tmp_path/"lease.db")); project=repo.create_project("租约")["id"]
    with pytest.raises(LLMError):
        IngestionPipeline(repo,FixtureLLMClient(recorder,{}),model_identity="resume").ingest(project,1,"一","原文")
    recovery=RecoveryService(repo); job=recovery.jobs(project)[0]
    with repo.db.transaction() as conn:
        conn.execute("UPDATE ingestion_jobs SET status='running',lease_until=?,owner='active' WHERE id=?",(time.time()+900,job["id"]))
    with pytest.raises(StorageError,match="正在运行"):
        recovery.resume(project,job["id"],FixtureLLMClient(recorder,{}))
    with repo.db.transaction() as conn: conn.execute("UPDATE ingestion_jobs SET lease_until=0 WHERE id=?",(job["id"],))
    payload={"summary":"新稿", "summary_evidence":[{"paragraph_id":"p0001","quote":"新稿"}]}
    IngestionPipeline(repo,FixtureLLMClient(recorder,{"b0001":payload}),model_identity="resume").ingest(project,1,"一","新稿")
    with pytest.raises(StorageError,match="不能.*回滚"):
        recovery.resume(project,job["id"],FixtureLLMClient(recorder,{}))


def test_backup_restore_and_project_export_round_trip_without_overwrite(revised_book,tmp_path):
    b=revised_book; archive=ArchiveService(b["repo"])
    other=b["repo"].create_project("不应泄露到单作品导出")["id"]
    ingest(b["repo"],b["recorder"],other,1,"其他作品专用口令")
    backup=tmp_path/"backup.db"; restored=tmp_path/"restored.db"
    archive.backup(backup)
    result=archive.restore(backup,restored)
    assert not result["active_database_replaced"]
    restored_repo=Repository(Database(restored))
    assert restored_repo.records(b["project"],"fact")==b["repo"].records(b["project"],"fact")
    with pytest.raises(StorageError): archive.backup(backup)
    with pytest.raises(StorageError): archive.restore(backup,b["repo"].db.path)
    export=tmp_path/"knowledge.json"; archive.export(b["project"],export)
    raw=export.read_text(encoding="utf-8")
    assert "其他作品专用口令" not in raw and "不应泄露到单作品导出" not in raw
    data=json.loads(raw)
    assert data["current_only"] and len(data["records"]["fact"])==4
    # 校验清单损坏不能静默恢复。
    manifest=json.loads((tmp_path/"backup.db.json").read_text(encoding="utf-8")); manifest["sha256"]="wrong"
    (tmp_path/"backup.db.json").write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(StorageError,match="校验清单"):
        archive.restore(backup,tmp_path/"bad-restore.db")


def test_statistics_counts_failed_attempts_and_marks_missing_usage(revised_book,tmp_path):
    b=revised_book; path=tmp_path/"calls.jsonl"
    rows=[{"call_id":"one","attempt":1,"provider":"deepseek","model":"test","status":"timeout","usage":{}},
          {"call_id":"one","attempt":2,"provider":"deepseek","model":"test","status":"success","usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}]
    path.write_text("\n".join(json.dumps(r) for r in rows)+"\ninvalid\n",encoding="utf-8")
    result=StatisticsService(b["repo"]).summarize([path,path])
    group=result["groups"][0]
    assert group["attempts"]==2 and group["unique_calls"]==1 and group["failed_attempts"]==1
    assert group["known_total_tokens"]==15 and group["missing_total_tokens_attempts"]==1 and result["malformed_lines"]==1
    scoped=StatisticsService(b["repo"]).summarize([path],project=b["project"])
    assert not scoped["groups"] and scoped["unattributed_attempts"]==2


def test_maintenance_cli_status_backup_and_statistics(revised_book,tmp_path,capsys):
    b=revised_book; args=["--db",str(b["repo"].db.path)]
    assert main(["revision","status",*args,"--project",b["project"]])==0
    assert json.loads(capsys.readouterr().out)["pending_total"]==0
    assert main(["archive","backup",*args,"--output",str(tmp_path/"cli.db")])==0
    assert json.loads(capsys.readouterr().out)["metadata"]["integrity_check"]=="ok"
    assert main(["usage-stats",*args,"--project",b["project"],"--log",str(tmp_path/"missing.jsonl")])==0
    assert json.loads(capsys.readouterr().out)["missing_logs"]


def test_v5_upgrade_retains_data_and_backup(tmp_path):
    path=tmp_path/"v5.db"
    with sqlite3.connect(path) as conn:
        for name in ("schema.sql","migration_002.sql","migration_003.sql","migration_004.sql","migration_005.sql"):
            conn.executescript(files("easy_writing.storage").joinpath(name).read_text(encoding="utf-8"))
        conn.execute("INSERT INTO projects VALUES('old','旧项目','old')")
        conn.execute("PRAGMA user_version=5")
    db=Database(path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == Database.VERSION and not conn.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(str(path)+".pre-v6.bak") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0]==5


def test_unchanged_evidence_migration_releases_dependents_and_is_reader_visible(revised_book):
    b=revised_book
    text,payload=fixture()
    # 新增无关段落，原知识及证据段落都保持原样。
    IngestionPipeline(b["repo"],FixtureLLMClient(b["recorder"],{"b0001":payload}),model_identity="revision-fixture-v1").ingest(b["project"],12,"旧宅",text+"\n\n院外传来风声。")
    assert not b["revision"].status(b["project"])["pending_total"]
    visible=b["knowledge"].facts(b["project"],actor="林月",predicate="密码",through_chapter=13,at_event=b["events"][13])
    # 原获知记录未设置读者揭示边界，作者视图仍可核验，且事件 ID 映射对读者可见。
    from easy_writing.services.timeline import event_root
    with b["repo"].db.connect() as conn:
        new=next(r["id"] for r in b["repo"].records(b["project"],"event") if r["chapter_no"]==12)
        assert event_root(conn,b["events"][12],13)==new
    assert b["knowledge"].facts(b["project"],actor="林月",predicate="密码")["facts"]
    assert not visible["coverage"]["quarantined_chapters"]


def test_ambiguous_same_event_evidence_is_not_auto_aligned(revised_book):
    b=revised_book; original=b["events"][12]
    with b["repo"].db.transaction() as conn:
        row=conn.execute("SELECT * FROM events WHERE id=?",(original,)).fetchone(); duplicate=uid()
        conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",(duplicate,row["generation_id"],row["title"],row["summary"],row["story_time"],row["review_status"]))
        for entity in conn.execute("SELECT entity_id FROM event_entities WHERE event_id=?",(original,)).fetchall():
            conn.execute("INSERT INTO event_entities VALUES(?,?)",(duplicate,entity[0]))
        for e in conn.execute("SELECT * FROM evidence_links WHERE record_type='event' AND record_id=?",(original,)).fetchall():
            conn.execute("INSERT INTO evidence_links VALUES(?,?,?,?,?,?,?)",(uid(),"event",duplicate,e["source_chunk_id"],e["quote"],e["quote_start"],e["quote_end"]))
    text,payload=fixture()
    IngestionPipeline(b["repo"],FixtureLLMClient(b["recorder"],{"b0001":payload}),model_identity="revision-fixture-v1").ingest(b["project"],12,"旧宅",text+"\n\n新段落。")
    with b["repo"].db.connect() as conn:
        assert not conn.execute("SELECT 1 FROM record_successions WHERE kind='event' AND old_id=?",(original,)).fetchone()
    assert 12 in b["knowledge"].facts(b["project"])["coverage"]["quarantined_chapters"]


def test_resume_cannot_replace_newer_extractor_generation(revised_book):
    b=revised_book
    old=next(j for j in RecoveryService(b["repo"]).jobs(b["project"]) if j["chapter_no"]==12)
    text,payload=fixture()
    IngestionPipeline(b["repo"],FixtureLLMClient(b["recorder"],{"b0001":payload}),model_identity="newer-extractor").ingest(b["project"],12,"旧宅",text)
    with pytest.raises(StorageError,match="新的提取代次"):
        RecoveryService(b["repo"]).resume(b["project"],old["id"],FixtureLLMClient(b["recorder"],{}))


def test_pending_fact_generation_is_excluded_from_current_export(revised_book,tmp_path):
    b=revised_book
    ingest(b["repo"],b["recorder"],b["project"],12,"白鹭")
    path=tmp_path/"current.json"; ArchiveService(b["repo"]).export(b["project"],path,include_sources=False)
    data=json.loads(path.read_text(encoding="utf-8"))
    assert data["coverage"]["quarantined_chapters"]==[13]
    assert "青石" not in json.dumps(data["records"],ensure_ascii=False)


def test_project_statistics_include_cached_and_failed_ingestion_attempt_ids(revised_book,tmp_path):
    b=revised_book
    with b["repo"].db.connect() as conn:
        call=conn.execute("SELECT call_id FROM ingestion_attempts LIMIT 1").fetchone()[0]
    path=tmp_path/"project_calls.jsonl"
    path.write_text(json.dumps({"call_id":call,"attempt":1,"provider":"deepseek","model":"test","status":"success","usage":{"total_tokens":42}})+"\n",encoding="utf-8")
    result=StatisticsService(b["repo"]).summarize([path],project=b["project"])
    assert result["groups"][0]["known_total_tokens"]==42 and not result["unattributed_attempts"]


def test_resume_preserves_original_output_budget_before_any_new_call(tmp_path,recorder,settings):
    repo=Repository(Database(tmp_path/"budget.db")); project=repo.create_project("原预算")["id"]
    original=FixtureLLMClient(recorder,{})
    original.settings=settings.model_copy(update={"max_output_tokens":4096})
    with pytest.raises(LLMError):
        IngestionPipeline(repo,original,model_identity="budget-v1").ingest(project,1,"一","原文")
    recovery=RecoveryService(repo); job=recovery.jobs(project)[0]
    assert recovery.output_budget(project,job["id"],1024)==4096
    response={"summary":"原文", "summary_evidence":[{"paragraph_id":"p0001","quote":"原文"}]}
    resumed=FixtureLLMClient(recorder,{"b0001":response}); resumed.settings=settings
    with pytest.raises(StorageError,match="模型配置"):
        recovery.resume(project,job["id"],resumed)
    resumed.settings=original.settings
    assert recovery.resume(project,job["id"],resumed)["model_calls"]==1
