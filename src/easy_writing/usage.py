"""每次尝试一条元数据日志，绝不保存完整请求或响应内容。"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from easy_writing.llm.base import LLMError


class CallRecorder(Protocol):
    def record(self, event: dict) -> None: ...


class JsonlCallRecorder:
    def __init__(self, path: Path, *, secrets: tuple[str, ...] = ()):
        self.path = path
        self._secrets = tuple(value for value in secrets if value)
        self._lock = threading.Lock()

    def record(self, event: dict) -> None:
        row = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        line = json.dumps(row, ensure_ascii=False, allow_nan=False)
        for secret in self._secrets:
            # 先按 JSON 编码后的形式替换，兼容引号与转义符。
            line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
        except OSError:
            # 成功响应后日志失败不得被当作网络失败而重新发起付费请求。
            raise LLMError(
                "用量日志写入失败，请检查 LLM_USAGE_LOG 路径和权限；"
                "若已收到响应，接口可能已经计费，请勿盲目重复请求。",
                code="usage_log_error",
            ) from None
