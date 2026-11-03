import pytest

from easy_writing.config import ENV_FIELDS, Settings


class MemoryRecorder:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


@pytest.fixture
def recorder():
    return MemoryRecorder()


@pytest.fixture
def settings():
    return Settings(api_key="sk-test-do-not-log", retry_base_seconds=0)


@pytest.fixture(autouse=True)
def isolated_cli_environment(monkeypatch, tmp_path):
    # 无论开发机是否有真实密钥，测试都不读取真实 .env 或触网。
    for name in ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
