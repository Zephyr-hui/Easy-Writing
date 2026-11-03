"""附带人工标准标注的内置样章，只供离线体验。"""

from easy_writing.ingestion.extractor import FixtureLLMClient
from easy_writing.ingestion.pipeline import IngestionPipeline
from easy_writing.services.checking import CheckingService
from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.timeline import TimelineService

TEXT = "林舟和林月站在旧宅铁门前。林舟清楚自己正独自持有唯一的钥匙，也知道地窖密码是青石；林月尚未获知密码。此世铁门绝不能被穿墙通过。门上留着一枚没有解释的月牙划痕。"


def import_sample(repo, project, recorder, chapter_no=1):
    evidence = [{"paragraph_id": "p0001", "quote": TEXT}]
    payload = {"summary": TEXT, "summary_evidence": evidence,
        "entities": [{"key": key, "name": name, "kind": kind, "evidence": evidence} for key, name, kind in (
            ("zhou", "林舟", "person"), ("yue", "林月", "person"), ("key", "钥匙", "item"),
            ("room", "地窖", "place"), ("door", "铁门", "item"), ("rule", "世界规则", "concept"))],
        "events": [{"key": "arrival", "title": "抵达旧宅", "summary": TEXT, "participants": ["zhou", "yue"], "evidence": evidence}],
        "facts": [
            {"key": "holder", "subject": "key", "predicate": "持有人", "object_value": "林舟", "object_entity": "zhou", "effective_from": "arrival", "evidence": evidence},
            {"key": "secret", "subject": "room", "predicate": "密码", "object_value": "青石", "effective_from": "arrival", "evidence": evidence},
            {"key": "rule", "subject": "rule", "predicate": "通行限制", "object_value": "铁门不得穿墙", "effective_from": "arrival", "evidence": evidence}],
        "foreshadowing": [{"key": "crescent", "title": "月牙划痕", "description": "门上的月牙划痕尚未解释。", "evidence": evidence}]}
    result = IngestionPipeline(repo, FixtureLLMClient(recorder, {"b0001": payload}), model_identity="stage6-human-sample-v1").ingest(project, chapter_no, "内置样章：旧宅门前", TEXT)
    # 样章已有人工答案；不对用户导入的任意正文自动确认。
    events = [e for e in repo.records(project, "event", limit=500) if e["chapter_no"] == chapter_no]
    event = next(e for e in events if e["title"] == "抵达旧宅")["id"]
    graph, knowledge = TimelineService(repo), KnowledgeService(repo)
    graph.set_time(event, start=0, revealed_at_chapter=chapter_no, reason="内置样稿人工标准标注")
    for fact in [f for f in repo.records(project, "fact", limit=500) if f["chapter_no"] == chapter_no]:
        repo.edit("fact", fact["id"], status="confirmed", reason="内置样稿人工标准标注")
        knowledge.bind_fact(fact["id"], from_event=event, mode="state" if fact["predicate"] == "持有人" else "assertion", reason="内置样稿人工标注")
        if fact["predicate"] == "持有人":
            knowledge.learn(fact["id"], "林舟", event, revealed_at_chapter=chapter_no, reason="样稿明确林舟知道自己正在持有钥匙")
        if fact["predicate"] == "密码":
            knowledge.learn(fact["id"], "林舟", event, revealed_at_chapter=chapter_no, reason="样稿原文明示林舟已知，林月未知")
        if fact["predicate"] == "通行限制":
            service = CheckingService(repo)
            # 内置样稿重复导入不会重复登记同一个规则。
            with repo.db.connect() as conn:
                existing = conn.execute("SELECT id FROM world_constraints WHERE project_id=? AND rule_fact_id=?", (project, fact["id"])).fetchone()
            if not existing:
                service.add_rule(project, fact["id"], "铁门", "穿墙", "forbid", "成功", reason="内置样稿人工规则")
    return {**result, "event_id": event, "real_model_calls": 0}
