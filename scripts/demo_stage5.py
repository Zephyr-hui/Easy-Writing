"""第五阶段离线验收：伏笔、矛盾、误报边界及分页；不调用真实模型。"""

import argparse
import json
import sys
from pathlib import Path

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.checking import CheckingService
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import TimelineService
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository, uid
from easy_writing.usage import JsonlCallRecorder


def extraction(chapter):
    evidence = [{"paragraph_id": "p0001", "quote": chapter["text"]}]
    return {"summary": chapter["text"], "summary_evidence": evidence,
        "entities": [{"key": key, "name": name, "kind": kind, "evidence": evidence} for key, name, kind in (
            ("zhou", "林舟", "person"), ("yue", "林月", "person"), ("key", "钥匙", "item"),
            ("room", "地窖", "place"), ("door", "铁门", "item"), ("rule", "世界规则", "concept"))],
        "events": [{"key": "event", "title": chapter["title"], "summary": chapter["text"],
            "mention_kind": chapter.get("mention_kind", "occurrence"), "evidence": evidence}],
        "facts": [{**fact, "key": f"f{i}", "effective_from": "event", "evidence": evidence} for i, fact in enumerate(chapter["facts"])],
        "foreshadowing": [{"key": f"t{i}", "title": title, "description": title, "evidence": evidence} for i, title in enumerate(chapter.get("threads", []))]}


def collect_pages(call, field, **kwargs):
    result, offset = [], 0
    while True:
        page = call(limit=37, offset=offset, **kwargs)
        result.extend(page[field])
        if page["next_offset"] is None:
            return result
        offset = page["next_offset"]


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stage5_demo/novel.db"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    book = json.loads((root / "examples/stage5/book.json").read_text(encoding="utf-8"))
    repo = Repository(Database(args.db))
    # 每次独立项目，避免重置作者状态与历史；不会覆盖用户作品。
    project = repo.create_project("第五阶段验收样稿-" + uid()[:10])["id"]
    recorder = JsonlCallRecorder(args.db.parent / "fixture_calls.jsonl")
    for chapter in book["chapters"]:
        client = FixtureLLMClient(recorder, {"b0001": extraction(chapter)})
        IngestionPipeline(repo, client, model_identity="stage5-human-fixture-v1").ingest(
            project, chapter["number"], chapter["title"], chapter["text"])
    for kind in ("event", "fact"):
        for row in repo.records(project, kind):
            repo.edit(kind, row["id"], status="confirmed", reason="验收样稿的人工标准标注")
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    checks, threads = CheckingService(repo), ForeshadowingService(repo)
    events = {r["chapter_no"]: r["id"] for r in repo.records(project, "event")}
    facts = repo.records(project, "fact")
    holders = {f["chapter_no"]: f["id"] for f in facts if f["predicate"] == "持有人"}
    secret = next(f["id"] for f in facts if f["predicate"] == "密码")
    rule = next(f["id"] for f in facts if f["predicate"] == "通行限制")
    for chapter in book["chapters"]:
        graph.set_time(events[chapter["number"]], start=chapter["tick"], revealed_at_chapter=chapter["number"], reason="人工标准故事时间")
    knowledge.bind_fact(holders[1], from_event=events[1], until_event=events[2], reason="正常转交在边界终止旧状态")
    knowledge.bind_fact(holders[2], from_event=events[2], until_event=events[5], reason="原文明示持有直至销毁")
    knowledge.bind_fact(holders[3], from_event=events[3], reason="与明确持有区间冲突的新状态")
    knowledge.learn(secret, "林月", events[4], revealed_at_chapter=4, reason="原文明示首次获知")
    checks.record_use(project, secret, "林月", events[3], reason="原文明确输入该口令，而非普通提及")
    checks.add_rule(project, rule, "铁门", "穿墙", "forbid", "成功", reason="作者确认无条件规则与检查范围")
    initial_threads = threads.list(project)["threads"]
    main_thread = next(t["id"] for t in initial_threads if t["title"] == "门前划痕")
    fog = next(t["id"] for t in initial_threads if t["title"] == "远处雾气")
    threads.transition(project, main_thread, "planted", event_id=events[1], reason="作者确认埋设")
    threads.transition(project, fog, "open", reason="保留开放解释，不当作漏填")
    graph.link(events[5], events[1], "resolves", revealed_at_chapter=5, reason="后文部分解释划痕")
    graph.link(events[5], events[1], "before", reason="人为植入的错误时间候选")
    for number in range(123):
        threads.create(project, f"分页测试人工候选{number + 1:03}", "仅用于全量分页验收，不代表新增小说伏笔。", event_id=events[1], reason="人工分页测试数据")
    full_inventory = collect_pages(lambda **kw: threads.list(project, **kw), "threads")
    assert len(full_inventory) == len({t["id"] for t in full_inventory}) == 125
    first = checks.run(project)
    kinds = {f["kind"] for f in first["findings"]}
    assert kinds == {"state_conflict", "early_knowledge", "world_rule", "thread_suggestion", "temporal_candidate"}
    assert first["examined"]["threads"] == 125
    state_conflict = next(f for f in first["findings"] if f["kind"] == "state_conflict")
    assert f"fact:{holders[1]}" not in state_conflict["record_keys"]  # 正常转交未误报。
    assert not any(f["kind"] == "timeline_conflict" for f in first["findings"])  # 倒叙未误报。
    assert next(t for t in threads.list(project)["threads"] if t["id"] == main_thread)["state"] == "planted"
    early = checks.run(project, through_chapter=2)
    assert early["finding_total"] == 0  # 后文章节和关联不进入前文检查。
    awareness = next(f for f in first["findings"] if f["kind"] == "early_knowledge")
    checks.decide(project, awareness["id"], "intentional", reason="演示作者将此处标为有意叙述误导，待后文解释")
    repeated = checks.run(project)
    assert next(f for f in repeated["findings"] if f["kind"] == "early_knowledge")["status"] == "intentional"
    threads.transition(project, main_thread, "partially_resolved", event_id=events[5], reason="作者确认图案解释了一部分，但仍缺一角")
    final = checks.run(project)
    assert checks.report(project, first["id"])["stale"]
    all_findings = collect_pages(lambda **kw: checks.report(project, final["id"], **kw), "findings")
    assert len(all_findings) == final["finding_total"]
    acceptance = {"project_id": project, "database": str(args.db.resolve()), "check_run_id": final["id"],
        "first_run_id": first["id"], "detected_kinds": sorted(kinds), "full_inventory_count": 125,
        "normal_transfer_not_reported": True, "flashback_not_reported_as_error": True,
        "chapter_boundary_verified": True, "automatic_thread_change_applied": False,
        "author_intent_survived_rerun": True, "old_report_marked_stale": True,
        "final_open_issues": final["open_issues"], "real_model_calls": 0,
        "evaluation_basis": "人工标注的结构化样稿；不等于任意小说的自动提取或语义判断质量"}
    args.db.parent.mkdir(parents=True, exist_ok=True)
    (args.db.parent / "stage5_report.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.db.parent / "stage5_acceptance.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(acceptance, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
