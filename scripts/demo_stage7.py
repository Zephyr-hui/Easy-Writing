"""较长样稿整体验收：改稿、依赖、派生刷新、恢复与检索实测。"""

import argparse
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.retrieval.context import ContextBuilder, estimate_tokens
from easy_writing.retrieval.corpus import CorpusBuilder
from easy_writing.retrieval.embeddings import BGEEmbeddingProvider, FakeEmbeddingProvider
from easy_writing.retrieval.hybrid import HybridRetriever
from easy_writing.services.archive import ArchiveService
from easy_writing.services.checking import CheckingService
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.recovery import RecoveryService
from easy_writing.services.revisions import RevisionService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository, uid
from easy_writing.usage import JsonlCallRecorder


def chapter(number, password=None, *, referring=False):
    room_number = 12 if referring else number
    password = password or f"石灯{room_number:03}"
    first = f"林舟和林月抵达第{number:03}号旧宅，林舟取出第{number:03}把铜钥匙。门上刻着未解释的月牙划痕。"
    second = f"第{room_number:03}号地窖的通行口令是{password}。"
    # 固定小说式填充仅模拟正文规模，不作为额外事件的人工标准答案。
    padding = "风吹过院墙，灯影沿石阶移动。兄妹核对手中的记录，没有把猜想当作答案。" * 42
    text = first + "\n\n" + second + "\n\n" + padding
    ev1 = [{"paragraph_id": "p0001", "quote": first}]
    ev2 = [{"paragraph_id": "p0002", "quote": second}]
    payload = {"summary": first + second, "summary_evidence": ev1 + ev2,
        "entities": [{"key": key, "name": name, "kind": kind, "evidence": ev1} for key,name,kind in (
            ("zhou","林舟","person"),("yue","林月","person"),("key",f"第{number:03}把铜钥匙","item"),("room",f"第{room_number:03}号地窖","place"))],
        "events": [{"key":"visit","title":f"调查第{number:03}号旧宅","summary":first,"participants":["zhou","yue"],"evidence":ev1}],
        "facts": [{"key":"holder","subject":"key","predicate":"持有人","object_value":"林舟","object_entity":"zhou","effective_from":"visit","evidence":ev1},
                  {"key":"password","subject":"room","predicate":"密码","object_value":password,"effective_from":"visit","evidence":ev2}],
        "foreshadowing": ([{"key":"crescent","title":"月牙划痕","description":"旧宅门上未解释的刻痕","evidence":ev1}] if number==12 else [])}
    return text,payload


def import_chapter(repo, recorder, project, number, password=None):
    text,payload=chapter(number,password,referring=number==13)
    return IngestionPipeline(repo,FixtureLLMClient(recorder,{"b0001":payload}),model_identity="stage7-human-benchmark-v1").ingest(project,number,f"第{number}章",text)


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=Path("data/stage7_demo/novel.db"))
    parser.add_argument("--chapters",type=int,default=120)
    parser.add_argument("--embedding",choices=["fake","bge"],default="fake")
    args=parser.parse_args()
    if not 20 <= args.chapters <= 300:
        parser.error("样稿章节数应在 20～300 之间")
    repo=Repository(Database(args.db)); project=repo.create_project("第七阶段长样稿-"+uid()[:8])["id"]
    recorder=JsonlCallRecorder(args.db.parent/"stage7_fixture_calls.jsonl")
    started=perf_counter()
    for number in range(1,args.chapters+1):
        import_chapter(repo,recorder,project,number)
    import_ms=(perf_counter()-started)*1000
    facts=repo.records(project,"fact",limit=500)
    events={e["chapter_no"]:e["id"] for e in repo.records(project,"event",limit=500)}
    password={f["chapter_no"]:f["id"] for f in facts if f["predicate"]=="密码"}
    graph,knowledge=TimelineService(repo),KnowledgeService(repo)
    for number,event in events.items():
        graph.set_time(event,start=number,revealed_at_chapter=number,reason="人工标准故事刻度")
    for fact in facts:
        repo.edit("fact",fact["id"],status="confirmed",reason="人工标准标注")
    revision=RevisionService(repo)
    revision.depend(project,"fact",password[12],"fact",password[13],reason="第13章复用第12章口令")
    plot=graph.create_plot(project,"调查主线")["id"]
    for number in (11,12,14): graph.set_role(plot,events[number],"core",reason="主线骨架")
    graph.link(events[11],events[12],"before",reason="明确先后")
    graph.link(events[12],events[14],"before",reason="明确先后")
    graph.set_role(plot,events[12],"ordinary",reason="主线降级，保留事件与证据")
    thread=repo.records(project,"thread")[0]
    ForeshadowingService(repo).transition(project,thread["id"],"planted",event_id=events[12],reason="作者确认铺垫")
    before_report=CheckingService(repo).run(project)
    provider=FakeEmbeddingProvider() if args.embedding=="fake" else BGEEmbeddingProvider("BAAI/bge-small-zh-v1.5",cache_folder=Path(".cache/models"),revision="7999e1d3359715c523056ef9478215996d62a620")
    corpus=CorpusBuilder(repo).build(project)
    retriever=HybridRetriever(repo,provider)
    samples=[]
    for number in (7,12,27 if args.chapters>=27 else 17, args.chapters):
        question=f"第{number:03}号地窖开门时需要说什么通行短语？"
        started=perf_counter(); result=retriever.search(corpus,question,limit=5); elapsed=(perf_counter()-started)*1000
        packet=ContextBuilder(6000).build(corpus,question,result["hits"])
        ranked=[h["record"].key for h in result["hits"]]
        key=f"fact:{password[number]}"
        samples.append({"chapter":number,"rank_in_top5":ranked.index(key)+1 if key in ranked else None,
                        "latency_ms":round(elapsed,2),"context_estimated_tokens":packet["estimated_tokens"]})
    # 核心验收：第12章改稿，后文依赖保持隔离，旧向量仍保留但不能参与当前候选。
    import_chapter(repo,recorder,project,12,"白鹭012")
    current=knowledge.facts(project,subject="第012号地窖",predicate="密码")
    assert [f["object_name"] for f in current["facts"]]==["白鹭012"]
    assert 13 in current["coverage"]["quarantined_chapters"]
    assert CheckingService(repo).report(project,before_report["id"])["stale"]
    changed_corpus=CorpusBuilder(repo).build(project)
    assert not any("石灯012" in r.text for r in changed_corpus.records)
    preserved=next(e for e in graph.plot_view(plot,expanded=True)["events"] if "012" in e["title"])
    assert preserved["role"]=="ordinary" and repo.evidence("event",preserved["id"])
    # 作者实际改写第13章，再登记新依赖，不把旧值自动改成新值。
    import_chapter(repo,recorder,project,13,"白鹭012")
    for review in revision.status(project,limit=500)["reviews"]:
        revision.resolve(project,review["id"],reason="作者已对照第12、13章新版本重建口令和后文依据")
    new_passwords={f["chapter_no"]:f["id"] for f in repo.records(project,"fact",limit=500) if f["predicate"]=="密码"}
    revision.depend(project,"fact",new_passwords[12],"fact",new_passwords[13],reason="新稿继续共用新口令")
    refreshed=revision.refresh(project,provider=provider)
    repeat=revision.refresh(project,provider=provider)
    assert repeat["vectors"]["new_segments"]==0
    assert not CheckingService(repo).report(project,refreshed["check_run_id"])["stale"]
    # 恢复验收也使用同一项目的独立测试章：第一块成功，第二块缺失后显式继续。
    number=args.chapters+1; body="甲"*180+"\n\n"+"乙"*180
    one={"summary":"恢复测试首段","summary_evidence":[{"paragraph_id":"p0001","quote":"甲"*20}]}
    two={"summary":"恢复测试后段","summary_evidence":[{"paragraph_id":"p0002","quote":"乙"*20}]}
    try:
        IngestionPipeline(repo,FixtureLLMClient(recorder,{"b0001":one}),model_identity="stage7-resume-v1",max_chars=200).ingest(project,number,"恢复验收",body)
    except Exception as error:
        from easy_writing.llm.base import LLMError
        if not isinstance(error,LLMError): raise
    job=next(j for j in RecoveryService(repo).jobs(project) if j["chapter_no"]==number)
    resumed=RecoveryService(repo).resume(project,job["id"],FixtureLLMClient(recorder,{"b0002":two}))
    assert resumed["model_calls"]==1 and RecoveryService(repo).resume(project,job["id"],FixtureLLMClient(recorder,{}))["cached"]
    archive=ArchiveService(repo); tag=uid()[:8]
    backup=args.db.parent/("backup-"+tag+".db"); restored=args.db.parent/("restored-"+tag+".db")
    archive.backup(backup); archive.restore(backup,restored)
    restored_repo=Repository(Database(restored))
    assert len(restored_repo.records(project,"fact",limit=500))==len(repo.records(project,"fact",limit=500))
    archive.export(project,args.db.parent/("knowledge-"+tag+".json"))
    source_text="".join(chapter(i,referring=i==13)[0] for i in range(1,args.chapters+1))
    warm=[s["latency_ms"] for s in samples[1:]]
    report={"project_id":project,"database":str(args.db.resolve()),"chapters":args.chapters,"source_characters":len(source_text),
        "source_input_estimate":estimate_tokens(source_text),"input_estimator":"UTF-8 bytes, not provider billing tokenizer",
        "knowledge_records":len(corpus.records),"embedding_backend":args.embedding,"model":provider.metadata,
        "model_load_ms":round(getattr(provider,"load_ms",0),2),"fixture_import_ms":round(import_ms,2),"retrieval_samples":samples,
        "warm_query_median_ms":round(statistics.median(warm),2),"warm_query_max_ms":max(warm),
        "recall_at5":sum(s["rank_in_top5"] is not None for s in samples)/len(samples),
        "changed_chapter_hidden_old_fact":True,"dependent_chapter_quarantined_until_rewrite":True,
        "ordinary_node_and_evidence_preserved":True,"old_report_stale":True,"repeat_index_new_segments":repeat["vectors"]["new_segments"],
        "resume_missing_blocks_calls":resumed["model_calls"],"backup_restore_verified":True,"backup_path":str(backup.resolve()),
        "real_model_calls":0,"quality_basis":"人工提取标准样稿的检索与工程验证；fake 不评估真实语义能力",
        "storage_decision":"当前规模继续 SQLite + NumPy；更大语料需以相同评测重测，不据此承诺任意长篇性能"}
    name="stage7_bge_acceptance.json" if args.embedding=="bge" else "stage7_acceptance.json"
    (args.db.parent/name).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__": main()
