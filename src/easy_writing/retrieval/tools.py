from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from easy_writing.retrieval.context import ContextBuilder
from easy_writing.retrieval.corpus import chapter_records
from easy_writing.retrieval.hybrid import terms_for
from easy_writing.storage.database import StorageError


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class SourceArgs(Args):
    keys: list[str] = Field(min_length=1, max_length=3)
    version_id: str | None = None


class ChainArgs(Args):
    event_id: str


class RelatedArgs(ChainArgs):
    direction: Literal["in", "out", "both"] = "both"
    relation: Literal["before", "overlaps", "during", "causes", "foreshadows", "resolves"] | None = None


class ChapterArgs(Args):
    chapter: int = Field(gt=0)


class CoverageArgs(Args):
    chapter: int | None = Field(default=None, gt=0)


class CharacterArgs(Args):
    character: str = Field(min_length=1, max_length=160)
    topic: str | None = Field(default=None, max_length=240)


class EventArgs(Args):
    event: str = Field(min_length=1, max_length=160)
    chapter: int | None = Field(default=None, gt=0)


class ThreadArgs(Args):
    state: Literal["candidate", "planted", "reinforced", "partially_resolved", "resolved", "cancelled", "open"] | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=8)


class ReadOnlyTools:
    schemas = {"search_memory": SearchArgs, "read_source": SourceArgs,
               "get_event_chain": ChainArgs, "get_ingestion_status": Args}

    def __init__(self, corpus, retriever, reader, question, *, budget=2400, conversation=False,
                 model_first=False, allow_pending=False):
        self.corpus, self.retriever, self.reader = corpus, retriever, reader
        self.question, self.budget = question, budget
        self.model_first = model_first
        if conversation or model_first:
            self.schemas = self.schemas | {"get_related_events": RelatedArgs}
        if model_first:
            self.schemas = self.schemas | {"get_chapter_overview": ChapterArgs, "get_character_details": CharacterArgs,
                                           "get_event_details": EventArgs, "get_ingestion_status": CoverageArgs,
                                           "list_foreshadowing": ThreadArgs}
            if allow_pending and corpus.actor is None and corpus.at_event is None:
                self.schemas["read_pending_source"] = ChapterArgs

    @property
    def definitions(self):
        descriptions = {
            "search_memory": "在本次固定项目、版本、章节和角色范围中补充检索知识。",
            "read_source": "核对指定知识编号的局部原文，可指定其可见正文版本；角色认知范围禁止扩读。",
            "get_event_chain": "查询当前可见事件的已确认前置路径，不改变时间线。",
            "get_related_events": "按方向和关系类型查询可见事件的已确认连接。每次最多两跳、八个节点。",
            "get_ingestion_status": "查看本次任务范围内的入库覆盖和过期章节。",
            "get_chapter_overview": "按章号查指定一章的摘要和事件，例如第一章写了什么。缺章时返回入库状态。角色视角只返回该人物可见事实。",
            "get_character_details": "按人物姓名、别名或明确 ID 查身份、关系、相关事件和状态；同名时返回候选。",
            "get_event_details": "按事件标题或编号查参与者、故事时间、首次提及与依据；可指定正文章号。",
            "read_pending_source": "作者已授权局部读取指定未入库章节。返回有限片段，作为临时文字依据。",
            "list_foreshadowing": "按状态分页盘点当前可见伏笔与出处。返回总数与后续页位置。",
        }
        return [{"type": "function", "function": {"name": name, "description": descriptions[name],
                 "parameters": schema.model_json_schema()}} for name, schema in self.schemas.items()]

    def execute(self, name, arguments, *, evidence_keys=None):
        if name not in self.schemas:
            raise StorageError("工具不在只读白名单中。")
        try:
            args = self.schemas[name].model_validate(arguments)
        except ValidationError:
            raise StorageError("工具参数无效，或包含试图修改项目/角色范围的字段。") from None
        if name == "get_ingestion_status":
            if self.model_first and args.chapter is not None:
                return {"chapter": args.chapter, "status": self.chapter_status(args.chapter)}
            return {"coverage": self.corpus.coverage}
        if name == "search_memory":
            result = self.retriever.search(self.corpus, args.query, limit=args.limit)
            return ContextBuilder(self.budget).build(self.corpus, args.query, result["hits"])["packet"]
        if name == "read_source":
            if self.model_first and not set(args.keys) <= set(evidence_keys or ()):
                raise StorageError("请先检索到需要回查的证据编号。")
            sources = self.reader.read(args.keys, version_id=args.version_id)
            return ContextBuilder(self.budget).build(self.corpus, self.question, [], sources=sources)["packet"]
        if name == "get_chapter_overview":
            status = self.chapter_status(args.chapter)
            records = chapter_records(self.corpus, args.chapter)
            return self.detail_packet(f"第 {args.chapter} 章概述", records,
                                      chapter=args.chapter, status=status, matching_records=len(records))
        if name == "get_character_details":
            return self.character_details(args.character.strip(), args.topic or self.question)
        if name == "list_foreshadowing":
            if self.corpus.actor is not None or self.corpus.at_event is not None:
                return {"status": "scope_restricted", "message": "当前视角或故事时点范围不提供全章伏笔盘点。"}
            records = [record for record in self.corpus.records if record.kind == "thread"
                       and (args.state is None or record.extra.get("state") == args.state)]
            page = records[args.offset:args.offset + args.limit]
            packet = self.detail_packet("伏笔盘点", page, total=len(records))
            count = len(packet["records"])
            if count and args.offset + count < len(records):
                packet["detail"]["next_offset"] = args.offset + count
            return packet
        if name == "get_event_details":
            if args.chapter is not None:
                self.chapter_status(args.chapter)
            return self.event_details(args.event.strip(), args.chapter)
        if name == "read_pending_source":
            if self.chapter_status(args.chapter) != "not_current":
                raise StorageError("该章没有可读取的待入库正文。")
            sources = self.reader.pending("", chapter=args.chapter)
            return ContextBuilder(self.budget).build(self.corpus, f"第 {args.chapter} 章临时原文", [], sources=sources)["packet"]
        if self.model_first and self.corpus.actor is not None:
            raise StorageError("人物认知视角仅提供该人物的可见事实。")
        event_id = args.event_id.removeprefix("event:")
        if event_id not in self.corpus.snapshot["events"]:
            raise StorageError("事件不在当前可见范围。")
        paths = (self.reader.paths([event_id], direction=args.direction, relation=args.relation, details=True)
                 if name == "get_related_events" else self.reader.paths([event_id]))
        return ContextBuilder(self.budget).build(self.corpus, self.question, [], paths=paths)["packet"]

    def chapter_status(self, chapter):
        if self.corpus.through_chapter is not None and chapter > self.corpus.through_chapter:
            raise StorageError("章号超出本轮允许的正文章节范围。")
        for field, status in (("not_current", "not_current"), ("quarantined_chapters", "needs_review"),
                              ("loaded_chapters", "published")):
            if chapter in self.corpus.coverage[field]:
                return status
        return "missing"

    def detail_packet(self, label, records, **details):
        context = ContextBuilder(self.budget - 200)
        return context.build(self.corpus, label, [{"record": r} for r in records[:8]])["packet"] | {"detail": details}

    def character_details(self, value, topic):
        restricted = self.corpus.actor is not None or self.corpus.at_event is not None
        visible = {entity for record in self.corpus.records for entity in record.entity_ids}
        if self.corpus.actor:
            visible.add(self.corpus.actor)
        people = [person for person in self.corpus.catalog.values() if person["kind"] == "person"
                  and (not restricted or person["id"] in visible)]
        selected = [p for p in people if p["id"] == value.removeprefix("entity:") or p["name"] == value
                    or (not restricted and value in p["aliases"])]
        if len(selected) != 1:
            return {"status": "ambiguous" if selected else "not_found",
                    "candidates": [{"id": p["id"], "name": p["name"]} for p in selected[:5]]}
        person = selected[0]
        is_viewpoint = person["id"] == self.corpus.actor
        records = [r for r in self.corpus.records if person["id"] in r.entity_ids or (is_viewpoint and r.kind == "fact")]
        terms = terms_for(topic)
        records.sort(key=lambda r: (r.kind != "entity", -sum(term in r.text.lower() for term in terms),
                                    r.kind != "fact"))
        return self.detail_packet("人物详情", records, character_id=person["id"],
                                  matching_records=len(records), restricted=restricted)

    def event_details(self, value, chapter):
        if self.corpus.actor is not None:
            return {"status": "scope_restricted", "message": "人物认知视角下请查询人物可见事实。"}
        events = self.corpus.snapshot["events"]
        records = []
        for record in self.corpus.records:
            if record.kind == "event" and (record.id == value.removeprefix("event:") or events[record.id]["title"] == value):
                refs = [ref for ref in record.evidence if chapter is None or ref["chapter_no"] == chapter]
                if refs:
                    records.append(replace(record, evidence=refs))
        if len(records) != 1:
            return {"status": "ambiguous" if records else "not_found",
                    "candidates": [{"id": r.id, "title": events[r.id]["title"], "chapter": r.evidence[0]["chapter_no"]} for r in records[:5]]}
        event = records[0]
        facts = [r for r in self.corpus.records if r.kind == "fact" and event.id in r.event_ids]
        if chapter is not None:
            facts = [replace(r, evidence=[ref for ref in r.evidence if ref["chapter_no"] == chapter]) for r in facts]
            facts = [r for r in facts if r.evidence]
        return self.detail_packet("事件详情", [event, *facts], event_id=event.id)


def citation_keys(packet):
    keys = {r["key"] for r in packet.get("records", [])}
    keys.update(source["citation"] for source in packet.get("sources", []))
    keys.update(node["key"] for path in packet.get("paths", []) for node in path["nodes"])
    keys.update(link["key"] for path in packet.get("paths", []) for link in path["links"] if "key" in link)
    return keys
