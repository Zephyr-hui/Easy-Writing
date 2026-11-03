from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel

from easy_writing.schemas import LLMResult

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """仅含可安全展示的信息，不携带密钥、请求正文或远端错误正文。"""

    def __init__(self, message: str, *, code: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LLMClient(ABC):
    @abstractmethod
    def generate_text(self, prompt: str, *, system_prompt: str = "") -> LLMResult[str]:
        pass

    @abstractmethod
    def extract_json(
        self, prompt: str, schema: type[T], *, system_prompt: str = ""
    ) -> LLMResult[T]:
        pass

    def close(self) -> None:
        pass

    def call_tools(self, messages: list[dict], tools: list[dict], *, tool_choice="auto"):
        raise LLMError("此模型适配器尚未实现原生工具调用。", code="unsupported_tools")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
