"""无需 API Key 的第三阶段验收：倒叙、认知、事件对齐与节点降级。"""

import argparse
import json
import sys
from pathlib import Path

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import TimelineService, event_root
from easy_writing.storage.database import Database
from easy_writing.storage.repository import Repository
from easy_writing.usage import JsonlCallRecorder


def fixture(chapter):
    evidence = [{"paragraph_id": "p0001", "quote": chapter["text"]}]
    entities = [{"key": key, "name": name, "kind": kind, "evidence": evidence} for key, name, kind in (
        ("p1", "林舟", "person"), ("p2", "林月", "person"), ("key1", "钥匙", "item"), ("room", "地窖", "place"))]
    events = [{"key": e["key"], "title": e["title"], "summary": e["quote"],
               "mention_kind": e.get("mention_kind", "occurrence"),
               "evidence": [{"paragraph_id": "p0001", "quote": e["quote"]}]} for e in chapter["events"]]
    facts = []
    if chapter.get("holder"):
        holder = chapter["holder"]
        facts.append({"key": "holder", "subject": "key1", "predicate": "持有人", "object_entity": holder,
                      "object_value": "林舟" if holder == "p1" else "林月", "effective_from": events[0]["key"], "evidence": evidence})
    if chapter["number"] == 1:
        facts.append({"key": "sister", "subject": "p1", "predicate": "妹妹", "object_entity": "p2", "object_value": "林月", "evidence": evidence})
    if chapter.get("secret"):
        facts.append({"key": "password", "subject": "room", "predicate": "密码", "object_value": "青石",
                      "evidence": [{"paragraph_id": "p0001", "quote": "地窖密码：青石"}]})
    return {"summary": chapter["text"], "summary_evidence": evidence, "entities": entities, "events": events, "facts": facts}


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/stage3_demo.db"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    repo = Repository(Database(args.db))
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    project = repo.create_project("第三阶段节选样稿")["id"]
    recorder = JsonlCallRecorder(args.db.parent / "stage3_fixture_calls.jsonl")
    book = json.loads((root / "examples/stage3/book.json").read_text(encoding="utf-8"))
    imports = []
    for chapter in book["chapters"]:
        client = FixtureLLMClient(recorder, {"b0001": fixture(chapter)})
        imports.append(IngestionPipeline(repo, client, model_identity="stage3-human-fixture-v1").ingest(
            project, chapter["number"], chapter["events"][0]["title"], chapter["text"]))
    events = {e["title"]: e["id"] for e in repo.records(project, "event")}
    order = ["取得钥匙", "回忆修复", "交接钥匙", "怀疑地窖", "告知密码"]
    reveal = {"取得钥匙": 1, "回忆修复": 20, "交接钥匙": 5, "怀疑地窖": 10, "告知密码": 20}
    for tick, title in enumerate(order):
        graph.set_time(events[title], start=tick, revealed_at_chapter=reveal[title], reason="样稿作者的相对时间刻度")
    for left, right in zip(order, order[1:]):
        graph.link(events[left], events[right], "before", revealed_at_chapter=max(reveal[left], reveal[right]), reason="样稿明确顺序")
    graph.link(events["取得钥匙"], events["交接钥匙"], "before", revealed_at_chapter=5, reason="第五章前已知的交接顺序")
    with repo.db.connect() as conn:
        if event_root(conn, events["复述交接"]) != event_root(conn, events["交接钥匙"]):
            graph.merge_events(events["复述交接"], events["交接钥匙"], reason="复述同一交接事件", revealed_at_chapter=25)
    facts = repo.records(project, "fact")
    first = next(f["id"] for f in facts if f["predicate"] == "持有人" and f["chapter_no"] == 1)
    second = next(f["id"] for f in facts if f["predicate"] == "持有人" and f["chapter_no"] == 5)
    secret = next(f["id"] for f in facts if f["predicate"] == "密码")
    knowledge.bind_fact(first, from_event=events["取得钥匙"], until_event=events["交接钥匙"], reason="作者标注持有区间")
    knowledge.bind_fact(second, from_event=events["交接钥匙"], reason="作者标注后续持有状态")
    knowledge.bind_fact(secret, from_event=events["取得钥匙"], mode="assertion", reason="作者设定密码此前已存在")
    knowledge.learn(secret, "林月", events["告知密码"], revealed_at_chapter=20, reason="林月听到告知后才知道密码")
    main_plot = graph.create_plot(project, "调查主线")["id"]
    side_plot = graph.create_plot(project, "钥匙支线")["id"]
    for title in order:
        graph.set_role(main_plot, events[title], "ordinary" if title == "怀疑地窖" else "core", reason="样稿节点分层")
    graph.set_role(side_plot, events["回忆修复"], "core", reason="修复是支线核心")
    graph.set_role(main_plot, events["回忆修复"], "ordinary", reason="主线中降级为普通节点")
    overview = graph.plot_view(main_plot)
    before = knowledge.facts(project, subject="钥匙", predicate="持有人", at_event=events["取得钥匙"])["facts"]
    after = knowledge.facts(project, subject="钥匙", predicate="持有人", at_event=events["交接钥匙"])["facts"]
    reader = knowledge.answer(project, "第十章地窖密码是什么？")
    actor_before = knowledge.facts(project, actor="林月", subject="地窖", at_event=events["交接钥匙"])["facts"]
    actor_after = knowledge.facts(project, actor="林月", subject="地窖", at_event=events["告知密码"])["facts"]
    assert before[0]["object_name"] == "林舟" and after[0]["object_name"] == "林月"
    assert "青石" not in json.dumps(reader, ensure_ascii=False)
    assert not actor_before and actor_after[0]["object_name"] == "青石"
    assert any(path["via"] == [events["回忆修复"]] for path in overview["paths"])
    assert graph.plot_view(side_plot)["events"][0]["role"] == "core"
    assert len(repo.evidence("event", events["交接钥匙"])) == 2
    report = {"project_id": project, "database": str(args.db.resolve()), "main_plot_id": main_plot, "side_plot_id": side_plot,
              "holder_before": before[0]["object_name"], "holder_after": after[0]["object_name"],
              "reader_chapter10_secret_hidden": True, "actor_before_learning_unknown": True, "actor_after_learning": actor_after[0]["object_name"],
              "story_events": len(graph.timeline(project)["events"]), "narrative_mentions": len(graph.timeline(project, order="narrative")["mentions"]),
              "demotion_preserved_side_plot": True, "demotion_preserved_path": True,
              "cached_chapters": sum(i["cached"] for i in imports), "real_model_calls": 0}
    args.db.parent.mkdir(parents=True, exist_ok=True)
    (args.db.parent / "stage3_acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
