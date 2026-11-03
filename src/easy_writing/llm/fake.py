"""离线替身：返回固定演示数据，不声称能理解任意小说。"""

from copy import deepcopy
from uuid import uuid4

from pydantic import ValidationError

from easy_writing.llm.base import LLMClient, LLMError, T
from easy_writing.schemas import DEMO_JSON, LLMResult, TokenUsage
from easy_writing.usage import CallRecorder


class FakeLLMClient(LLMClient):
    def __init__(self, recorder: CallRecorder, *, json_payload: dict | None = None):
        self.recorder = recorder
        self.json_payload = deepcopy(DEMO_JSON if json_payload is None else json_payload)

    def _record(self, call_id: str, mode: str, status: str) -> None:
        self.recorder.record({
            "call_id": call_id, "provider": "fake", "model": "fake-v1",
            "mode": mode, "attempt": 1, "status": status, "latency_ms": 0,
            "usage": TokenUsage().model_dump(),
        })

    def generate_text(self, prompt: str, *, system_prompt: str = "") -> LLMResult[str]:
        if not prompt.strip():
            raise LLMError("输入内容不能为空。", code="empty_prompt")
        call_id = uuid4().hex
        self._record(call_id, "text", "success")
        return LLMResult(
            content="【离线假模型】文本接口运行正常。这是固定演示回复，未调用 DeepSeek。",
            provider="fake", model="fake-v1", usage=TokenUsage(), attempts=1, call_id=call_id,
        )

    def extract_json(
        self, prompt: str, schema: type[T], *, system_prompt: str = ""
    ) -> LLMResult[T]:
        if not prompt.strip():
            raise LLMError("输入内容不能为空。", code="empty_prompt")
        call_id = uuid4().hex
        try:
            content = schema.model_validate(deepcopy(self.json_payload))
        except ValidationError:
            self._record(call_id, "json", "schema_error")
            raise LLMError(
                "假模型固定数据不符合目标结构，请为测试提供匹配的 json_payload。",
                code="schema_error",
            ) from None
        self._record(call_id, "json", "success")
        return LLMResult(
            content=content, provider="fake", model="fake-v1", usage=TokenUsage(),
            attempts=1, call_id=call_id,
        )
