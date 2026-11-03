import json
from copy import deepcopy

from easy_writing.ingestion.models import ChapterExtraction
from easy_writing.llm.base import LLMError
from easy_writing.llm.fake import FakeLLMClient

PROMPT_VERSION = "chapter-v1"
SYSTEM_PROMPT = """你负责从小说分块中提取可核实的信息，输出 JSON。
只分析 paragraphs 的当前原文；known_entities 和 previous_summary 仅用于理解，不能作为本块证据。
每项证据须使用程序提供的 paragraph_id 和逐字 quote，不补写、改写或省略引用文字。
为实体、事件、事实分别设置局部唯一的英文 key，例如 person1、event1、fact1。
所有引用必须指向本次 JSON 内的 key；实体类型 person/organization/place/item/concept。
时间不明使用 null，不能把章节顺序当作故事时间或把推测写成明确事实。
同一人物的别名仅在原文明示时填写；同名不同身份设置 distinct=true。
回忆或复述用 mention_kind 标记，不自动推断为新的当下事件。
规则和人物关系也用 facts 表示；所有信息先作为未确认候选。
summary 为当前分块摘要，summary_evidence 提供依据。
没有相应内容时 entities/events/facts/links/foreshadowing 返回空数组。
foreshadowing 只列潜在悬念，不把普通细节必然认定为伏笔。
示例形状：{"summary":"这一块发生的事","summary_evidence":[{"paragraph_id":"p0001","quote":"原文短句"}],"entities":[],"events":[],"facts":[],"links":[],"foreshadowing":[]}。
"""


class FixtureLLMClient(FakeLLMClient):
    """按分块 ID 返回作者提供的离线数据；不会假装能提取任意小说。"""

    def __init__(self, recorder, blocks: dict):
        super().__init__(recorder)
        self.blocks = deepcopy(blocks)

    def extract_json(self, prompt, schema, *, system_prompt=""):
        block_id = json.loads(prompt)["block_id"]
        if block_id not in self.blocks:
            raise LLMError("离线 fixture 缺少当前分块的数据。", code="missing_fixture")
        self.json_payload = self.blocks[block_id]
        return super().extract_json(prompt, schema, system_prompt=system_prompt)
