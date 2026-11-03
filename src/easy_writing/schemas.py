"""厂商无关的结果和阶段一接口验收数据。"""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class TokenUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None
    # 保留接口提供的其他数值用量；不记录任意字符串或完整原始响应。
    numeric_details: dict[str, int | float | dict] = Field(default_factory=dict)


class LLMResult(BaseModel, Generic[T]):
    content: T
    provider: str
    model: str
    usage: TokenUsage
    attempts: int
    call_id: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict


class AssistantTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)


class DemoEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class DemoExtraction(BaseModel):
    """仅用于接口冒烟验收，尚非正式的章节提取数据结构。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    characters: list[str] = Field(min_length=1)
    events: list[DemoEvent] = Field(min_length=1)


DEMO_PASSAGE = "林舟把备用钥匙交给妹妹，随后独自离开了旧宅。"
DEMO_JSON = {
    "characters": ["林舟", "妹妹"],
    "events": [{"summary": "林舟将备用钥匙交给妹妹", "evidence": "林舟把备用钥匙交给妹妹"}],
}
