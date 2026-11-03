from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from easy_writing.ingestion.chunker import Block
from easy_writing.storage.database import StorageError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Key = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,39}$")]


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Draft):
    paragraph_id: str
    quote: str = Field(min_length=1)
    occurrence: int = Field(default=0, ge=0)


EvidenceList = Annotated[list[Evidence], Field(min_length=1, max_length=20)]


class EntityDraft(Draft):
    key: Key
    name: Text
    kind: Literal["person", "organization", "place", "item", "concept"]
    aliases: list[Text] = Field(default_factory=list, max_length=20)
    # 强制新建用于同名但不同身份的情况；未明确时只复用唯一精确匹配。
    distinct: bool = False
    evidence: EvidenceList


class EventDraft(Draft):
    key: Key
    title: Text
    summary: Text
    participants: list[Key] = Field(default_factory=list, max_length=30)
    story_time: Text | None = None
    mention_kind: Literal["occurrence", "recollection", "retelling", "uncertain"] = "occurrence"
    evidence: EvidenceList


class FactDraft(Draft):
    key: Key
    subject: Key
    predicate: Text
    object_value: Text
    object_entity: Key | None = None
    knowledge_holder: Key | None = None
    effective_from: Key | None = None
    effective_until: Key | None = None
    certainty: Literal["explicit", "inferred"] = "explicit"
    evidence: EvidenceList


class LinkDraft(Draft):
    source: Key
    target: Key
    relation: Literal["before", "overlaps", "during", "causes", "foreshadows", "resolves"]
    evidence: EvidenceList


class ThreadDraft(Draft):
    key: Key
    title: Text
    description: Text
    evidence: EvidenceList


class ChapterExtraction(Draft):
    summary: Text
    summary_evidence: EvidenceList
    entities: list[EntityDraft] = Field(default_factory=list, max_length=100)
    events: list[EventDraft] = Field(default_factory=list, max_length=100)
    facts: list[FactDraft] = Field(default_factory=list, max_length=100)
    links: list[LinkDraft] = Field(default_factory=list, max_length=100)
    foreshadowing: list[ThreadDraft] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def check_references(self):
        for items in (self.entities, self.events, self.facts, self.foreshadowing):
            if len({item.key for item in items}) != len(items):
                raise ValueError("分块内同类 key 必须唯一")
        entities = {item.key for item in self.entities}
        events = {item.key for item in self.events}
        for event in self.events:
            if not set(event.participants) <= entities:
                raise ValueError("事件引用不存在的实体")
        for fact in self.facts:
            if any(key not in entities for key in (fact.subject, fact.object_entity, fact.knowledge_holder) if key):
                raise ValueError("事实引用不存在的实体")
            if any(key not in events for key in (fact.effective_from, fact.effective_until) if key):
                raise ValueError("事实引用不存在的事件")
        for link in self.links:
            if link.source not in events or link.target not in events or link.source == link.target:
                raise ValueError("事件连接端点无效")
        return self


def locate_evidence(evidence: Evidence, block: Block) -> tuple[int, int]:
    paragraph = next((p for p in block.paragraphs if p.paragraph_id == evidence.paragraph_id), None)
    if paragraph is None or not evidence.quote.strip():
        raise StorageError("提取证据引用了不存在的段落或空白文本。")
    start = -1
    for _ in range(evidence.occurrence + 1):
        start = paragraph.text.find(evidence.quote, start + 1)
        if start < 0:
            raise StorageError("提取证据不逐字匹配对应原文，未发布本章知识。")
    return start, start + len(evidence.quote)


def validate_evidence(result: ChapterExtraction, block: Block) -> None:
    groups = [result.summary_evidence]
    for items in (result.entities, result.events, result.facts, result.links, result.foreshadowing):
        groups.extend(item.evidence for item in items)
    for group in groups:
        for evidence in group:
            locate_evidence(evidence, block)
    for entity in result.entities:
        # 推测别名不能直接写为身份对应关系。
        for alias in entity.aliases:
            if not any(alias in e.quote for e in entity.evidence):
                raise StorageError("实体别名缺少逐字原文依据，未发布本章知识。")
