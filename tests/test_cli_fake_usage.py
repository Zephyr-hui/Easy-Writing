import json
import subprocess
import sys

import pytest

from easy_writing.cli import main
from easy_writing.llm.base import LLMError
from easy_writing.llm.fake import FakeLLMClient
from easy_writing.schemas import DemoExtraction
from easy_writing.usage import JsonlCallRecorder


def test_fake_validates_and_records_unknown_usage(recorder):
    client = FakeLLMClient(recorder)
    result = client.extract_json("演示", DemoExtraction)
    assert result.provider == "fake"
    assert result.content.characters == ["林舟", "妹妹"]
    assert result.usage.total_tokens is None
    assert len(recorder.events) == 1


def test_fake_invalid_fixture_is_not_accepted(recorder):
    with pytest.raises(LLMError, match="不符合目标结构"):
        FakeLLMClient(recorder, json_payload={}).extract_json("演示", DemoExtraction)
    assert recorder.events[0]["status"] == "schema_error"


def test_fake_rejects_empty_prompt_without_logging(recorder):
    client = FakeLLMClient(recorder)
    with pytest.raises(LLMError):
        client.generate_text(" ")
    with pytest.raises(LLMError):
        client.extract_json(" ", DemoExtraction)
    assert recorder.events == []


def test_default_demo_runs_offline(capsys, tmp_path):
    assert main(["demo"]) == 0
    output = capsys.readouterr()
    assert "离线假模型" in output.err
    assert '"task": "text"' in output.out
    assert '"task": "json"' in output.out
    rows = [json.loads(line) for line in (tmp_path / "data/llm_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert {row["provider"] for row in rows} == {"fake"}


def test_doctor_does_not_create_call_log(tmp_path, capsys):
    assert main(["doctor"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["network_request_sent"] is False
    assert not (tmp_path / "data").exists()


def test_missing_key_has_clear_message(capsys):
    assert main(["demo", "--provider", "deepseek"]) == 1
    output = capsys.readouterr()
    assert "DEEPSEEK_API_KEY" in output.err
    assert "Traceback" not in output.err
    assert output.out == ""


def test_explicit_fake_overrides_provider_from_env(monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert main(["demo", "--provider", "fake", "--mode", "json"]) == 0
    assert '"provider": "fake"' in capsys.readouterr().out


def test_explicit_config_file_and_hidden_key(tmp_path, capsys):
    path = tmp_path / "custom.env"
    path.write_text("DEEPSEEK_API_KEY=do-not-print-this\nANALYSIS_MODEL=custom\n", encoding="utf-8")
    assert main(["doctor", "--env-file", str(path)]) == 0
    output = capsys.readouterr()
    assert "do-not-print-this" not in output.out + output.err
    assert '"api_key_configured": true' in output.out
    assert '"analysis_model": "custom"' in output.out


def test_nonexistent_explicit_file_is_an_error(capsys):
    assert main(["doctor", "--env-file", "missing.env"]) == 1
    assert "不存在" in capsys.readouterr().err


def test_ask_offline(capsys):
    assert main(["ask", "如何写小说？", "--provider", "fake"]) == 0
    assert "未调用 DeepSeek" in capsys.readouterr().out


def test_recorder_redacts_secrets_and_writes_utf8(tmp_path):
    log = tmp_path / "nested/usage.jsonl"
    recorder = JsonlCallRecorder(log, secrets=('sensitive"key',))
    recorder.record({"model": 'sensitive"key', "status": "测试"})
    event = json.loads(log.read_text(encoding="utf-8"))
    assert event["model"] == "[REDACTED]"
    assert event["status"] == "测试"
    assert "timestamp" in event


def test_deepseek_demo_cli_with_mock_http(monkeypatch, tmp_path, capsys):
    import httpx
    import easy_writing.cli as cli
    from easy_writing.llm.deepseek import DeepSeekClient
    from easy_writing.schemas import DEMO_JSON

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-local-test-only")
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        content = json.dumps(DEMO_JSON) if "response_format" in body else "可以辅助创作。"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def factory(settings, provider):
        assert provider == "deepseek"
        return DeepSeekClient(
            settings, JsonlCallRecorder(settings.usage_log),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(cli, "create_client", factory)
    assert main(["demo", "--provider", "deepseek"]) == 0
    output = capsys.readouterr()
    assert len(calls) == 2
    assert '"provider": "deepseek"' in output.out
    assert "sk-local-test-only" not in output.out + output.err
    rows = (tmp_path / "data/llm_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2


def test_demo_evidence_mismatch_is_reported(monkeypatch, recorder, capsys):
    import easy_writing.cli as cli
    payload = {
        "characters": ["林舟"],
        "events": [{"summary": "虚构事件", "evidence": "原文中不存在的句子"}],
    }
    monkeypatch.setattr(cli, "create_client", lambda *_: FakeLLMClient(recorder, json_payload=payload))
    assert main(["demo", "--provider", "fake", "--mode", "json"]) == 1
    assert "证据不匹配原文" in capsys.readouterr().err


def test_actual_module_entrypoint_outputs_utf8_chinese(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "easy_writing", "demo", "--provider", "fake"],
        cwd=tmp_path, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert "林舟" in result.stdout.decode("utf-8")
    assert "离线假模型" in result.stderr.decode("utf-8")
