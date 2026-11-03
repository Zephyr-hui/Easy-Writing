from dataclasses import dataclass, field, replace

from easy_writing.services.knowledge import KnowledgeService
from easy_writing.services.foreshadowing import ForeshadowingService
from easy_writing.services.review_scope import ReviewScope
from easy_writing.storage.database import StorageError


@dataclass
class MemoryRecord:
    kind: str
    id: str
    generation_id: str
    text: str
    evidence: list[dict] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    review_status: str = "unconfirmed"
    uncertainty: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.kind}:{self.id}"


@dataclass
class Corpus:
    project_id: str
    snapshot_id: str
    records: list[MemoryRecord]
    coverage: dict
    through_chapter: int | None
    actor: str | None
    at_event: str | None
    snapshot: dict
    catalog: dict


def chapter_records(corpus, chapter):
    """按正文出处查目标章，保留当前视角已经过滤过的记录。"""
    kinds = ("fact",) if corpus.actor else ("summary", "event")
    generations = {g for g, row in corpus.snapshot["generations"].items() if row["chapter_no"] == chapter}
    records = []
    for record in corpus.records:
        if record.kind in kinds and (record.kind == "fact" or record.generation_id in generations):
            refs = [ref for ref in record.evidence if ref["chapter_no"] == chapter]
            if refs:
                records.append(replace(record, evidence=refs))
    return sorted(records, key=lambda record: record.kind != "summary")


class CorpusBuilder:
    def __init__(self, repo):
        self.repo = repo
        self.knowledge = KnowledgeService(repo)

    def refs(self, conn, kind, ids, snap):
        allowed_versions = {c["knowledge_version_id"] for generation, c in snap["generations"].items()
                            if generation not in snap.get("quarantined_generations", ())}
        result = []
        for record_id in ids:
            for row in conn.execute("""SELECT e.id AS evidence_id,e.source_chunk_id,e.quote,e.quote_start,e.quote_end,
                s.version_id,s.paragraph_id,s.scene_no,s.start_offset,c.chapter_no
                FROM evidence_links e JOIN source_chunks s ON s.id=e.source_chunk_id
                JOIN chapter_versions v ON v.id=s.version_id JOIN chapters c ON c.id=v.chapter_id
                WHERE e.record_type=? AND e.record_id=? ORDER BY c.chapter_no,s.start_offset""", (kind, record_id)):
                if row["version_id"] in allowed_versions and (snap["through_chapter"] is None or row["chapter_no"] <= snap["through_chapter"]):
                    result.append(dict(row))
        return result

    def build(self, project_id, *, through_chapter=None, actor=None, at_event=None, subject=None, predicate=None, confirmed_only=False, details=False):
        with self.repo.db.connect() as conn:
            snap = self.knowledge.timeline._snapshot(conn, project_id, through_chapter)
            catalog = self.knowledge._catalog(conn, snap, current_only=details)
            facts = self.knowledge._facts(conn, snap, subject=subject, predicate=predicate, actor=actor, at_event=at_event, confirmed_only=confirmed_only)
            records = []
            for fact in facts:
                events = [e for e in (fact["effective_from"], fact["effective_until"]) if e in snap["events"]]
                text = f"{fact['subject_name']}｜{fact['predicate']}｜{fact['object_name']}"
                reviewed = conn.execute("SELECT COUNT(*) FROM edit_history WHERE record_type='fact' AND record_id=?", (fact["id"],)).fetchone()[0]
                records.append(MemoryRecord("fact", fact["id"], fact["generation_id"], text,
                    self.refs(conn, "fact", [fact["id"]], snap), [fact["subject_id"], *([fact["object_entity_id"]] if fact["object_entity_id"] else [])],
                    events, fact["review_status"], fact["uncertainties"],
                    {"mode": fact["mode"], "predicate": fact["predicate"], "subject_id": fact["subject_id"],
                     "object_name": fact["object_name"], "certainty": fact["certainty"], "author_reviewed": bool(reviewed)}))
            # 认知或精确状态查询仅使用已通过相应过滤的命题，防止摘要夹带其他时点/角色信息。
            if actor is None and subject is None and predicate is None:
                if details and at_event is None:
                    generation_by_version = {chapter["knowledge_version_id"]: generation
                                             for generation, chapter in snap["generations"].items()}
                    for person in catalog.values():
                        if person["kind"] != "person":
                            continue
                        mentions = [row[0] for source in person["source_ids"] for row in
                                    conn.execute("SELECT id FROM entity_mentions WHERE entity_id=?", (source,))]
                        refs = self.refs(conn, "entity_mention", mentions, snap)
                        if refs:
                            aliases = "、".join(sorted(person["aliases"] - {person["name"]}))
                            generation = generation_by_version[refs[0]["version_id"]]
                            records.append(MemoryRecord("entity", person["id"], generation, f"人物：{person['name']}｜别名：{aliases or '未记录'}",
                                                        refs, entity_ids=[person["id"]]))
                for event in snap["events"].values():
                    if at_event and self.knowledge._compare(snap, event["id"], at_event) not in ("equal", "before"):
                        continue
                    if confirmed_only and event["review_status"] != "confirmed":
                        continue
                    text = event["title"] + "：" + event["summary"]
                    participants = set()
                    if details:
                        for raw_id in event["source_event_ids"]:
                            for row in conn.execute("SELECT entity_id FROM event_entities WHERE event_id=?", (raw_id,)):
                                root = self.repo.canonical(conn, row[0])["id"]
                                if root in catalog:
                                    participants.add(root)
                        names = "、".join(sorted(catalog[p]["name"] for p in participants))
                        first = event["mentions"][0] if event["mentions"] else None
                        reveal = f"第{first['chapter_no']}章 {first['paragraph_id']}（{first['mention_kind']}）" if first else "未记录"
                        time = event["time"]
                        story = f"{time['frame']} {time['start_tick']}～{time['end_tick']}" if time else event["story_time"] or "未定"
                        text = f"{event['title']}｜参与者：{names or '未记录'}｜故事时间：{story}｜当前范围首次提及：{reveal}｜{event['summary']}"
                    records.append(MemoryRecord("event", event["id"], event["generation_id"], text,
                        self.refs(conn, "event", event["source_event_ids"], snap), entity_ids=sorted(participants),
                        event_ids=[event["id"]], review_status=event["review_status"]))
                if at_event is None:
                    thread_states = {r["id"]: r for r in ForeshadowingService._rows(ReviewScope(self.repo, conn, project_id, through_chapter))}
                    for generation in snap["generations"]:
                        if generation in snap.get("quarantined_generations", ()):
                            continue
                        for kind, table, fields in (("summary", "summaries", ("text",)), ("thread", "threads", ("title", "description"))):
                            for row in conn.execute(f"SELECT * FROM {table} WHERE generation_id=? AND review_status<>'rejected'", (generation,)):
                                from easy_writing.storage.revisions import pending
                                if pending(conn, kind, row["id"]):
                                    continue
                                if kind == "summary" and row["is_stale"]:
                                    continue
                                if confirmed_only and row["review_status"] != "confirmed":
                                    continue
                                thread = thread_states.get(row["id"]) if kind == "thread" else None
                                text = "：".join(row[f] for f in fields)
                                if thread:
                                    text += "｜状态：" + thread["state"]
                                records.append(MemoryRecord(kind, row["id"], generation, text,
                                    self.refs(conn, kind, [row["id"]], snap), review_status=row["review_status"],
                                    extra=({"candidate": thread["state"] == "candidate", "state": thread["state"], "state_needs_review": thread["state_needs_review"]} if thread else {})))
            return Corpus(project_id, snap["snapshot_id"], records, self.knowledge._coverage(conn, snap),
                          through_chapter, actor, at_event, snap, catalog)

    def check(self, corpus):
        with self.repo.db.connect() as conn:
            current = self.knowledge.timeline._snapshot(conn, corpus.project_id, corpus.through_chapter)["snapshot_id"]
        if current != corpus.snapshot_id:
            raise StorageError("知识库已变化，本次资料快照已过期，请重新查询。")
