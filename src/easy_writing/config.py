"""显式读取配置，不修改进程环境，不输出敏感字段。"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator


class ConfigurationError(Exception):
    """可安全展示的配置错误。"""


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True, frozen=True)

    provider: Literal["fake", "deepseek"] = "fake"
    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    base_url: str = "https://api.deepseek.com"
    extraction_model: str = Field(default="deepseek-flash", min_length=1)
    analysis_model: str = Field(default="deepseek-flash", min_length=1)
    extraction_thinking: Literal["enabled", "disabled"] = "disabled"
    analysis_thinking: Literal["enabled", "disabled"] = "disabled"
    timeout_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_base_seconds: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)
    max_output_tokens: int = Field(default=1024, ge=64, le=131072)
    usage_log: Path = Path("data/llm_calls.jsonl")
    database_path: Path = Path("data/easy_writing.db")
    chat_database_path: Path | None = None
    ocr_tessdata_path: Path = Path(".cache/tessdata")
    ocr_language: str = "chi_sim+eng"
    embedding_backend: Literal["bge", "fake"] = "bge"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_cache: Path = Path(".cache/models")
    embedding_revision: str | None = None
    retrieval_log: Path = Path("data/retrieval_calls.jsonl")

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        key = value.get_secret_value().strip()
        if any(not 33 <= ord(char) <= 126 for char in key):
            raise ValueError("密钥须为无空格的 ASCII 字符，请检查是否填入了占位说明")
        return SecretStr(key)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            url = urlsplit(value.strip())
            _ = url.port  # 触发非法端口校验。
            valid = (
                url.scheme == "https" and url.hostname and url.username is None
                and url.password is None and not url.query and not url.fragment
                and not any(char.isspace() for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("接口地址须为不含凭据、查询参数或片段的 HTTPS 地址")
        return value.rstrip("/")

    @field_validator("extraction_model", "analysis_model", "embedding_model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("模型名不能为空或含控制字符")
        return value

    def require_deepseek_key(self) -> None:
        if not self.api_key.get_secret_value():
            raise ConfigurationError(
                "未配置 DEEPSEEK_API_KEY。请在本机 .env 或环境变量中设置；"
                "离线试用可使用 --provider fake。"
            )


ENV_FIELDS = {
    "LLM_PROVIDER": "provider",
    "DEEPSEEK_API_KEY": "api_key",
    "DEEPSEEK_BASE_URL": "base_url",
    "EXTRACTION_MODEL": "extraction_model",
    "ANALYSIS_MODEL": "analysis_model",
    "EXTRACTION_THINKING": "extraction_thinking",
    "ANALYSIS_THINKING": "analysis_thinking",
    "LLM_TIMEOUT_SECONDS": "timeout_seconds",
    "LLM_MAX_RETRIES": "max_retries",
    "LLM_RETRY_BASE_SECONDS": "retry_base_seconds",
    "LLM_MAX_OUTPUT_TOKENS": "max_output_tokens",
    "LLM_USAGE_LOG": "usage_log",
    "DATABASE_PATH": "database_path",
    "CHAT_DATABASE_PATH": "chat_database_path",
    "OCR_TESSDATA_PATH": "ocr_tessdata_path",
    "OCR_LANGUAGE": "ocr_language",
    "EMBEDDING_BACKEND": "embedding_backend",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_CACHE": "embedding_cache",
    "EMBEDDING_REVISION": "embedding_revision",
    "RETRIEVAL_LOG": "retrieval_log",
}


def load_settings(
    env_file: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Settings:
    """默认读取当前工作目录 .env，环境变量覆盖文件；不向上寻找其他项目密钥。"""
    path = env_file if env_file is not None else Path.cwd() / ".env"
    try:
        file_values = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigurationError("无法读取配置文件，请检查文件权限和 UTF-8 编码。") from None
    env = os.environ if environ is None else environ
    values = {
        field: env[name] if name in env else file_values[name]
        for name, field in ENV_FIELDS.items()
        if name in env or file_values.get(name) is not None
    }
    try:
        return Settings.model_validate(values)
    except ValidationError as error:
        names = {field: name for name, field in ENV_FIELDS.items()}
        fields = ", ".join(sorted({names[str(item["loc"][0])] for item in error.errors()}))
        raise ConfigurationError(f"配置无效，请检查这些配置项：{fields}。") from None
