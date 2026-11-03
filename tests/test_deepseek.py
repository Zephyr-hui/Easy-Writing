import json

import httpx
import pytest

from easy_writing.llm.base import LLMError
from easy_writing.llm.deepseek import DeepSeekClient
from easy_writing.schemas import DEMO_JSON, DemoExtraction
from easy_writing.usage import JsonlCallRecorder


def response_body(content="你好", *, finish="stop", usage=True):
    result = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage:
        result["usage"] = {
            "prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28,
            "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 3}, "future_count": 4,
        }
    return result


def client_for(settings, recorder, handler, *, sleeper=lambda seconds: None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return DeepSeekClient(settings, recorder, http_client=http, sleeper=sleeper)


def test_text_payload_and_usage(settings, recorder):
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test-do-not-log"
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["stream"] is False
        assert body["max_tokens"] == 1024
        assert "response_format" not in body
        return httpx.Response(200, json=response_body())
    result = client_for(settings, recorder, handler).generate_text("问题")
    assert result.content == "你好"
    assert result.usage.cache_hit_tokens == 5
    assert result.usage.reasoning_tokens == 3
    assert result.usage.numeric_details["future_count"] == 4
    assert recorder.events[0]["status"] == "success"
    assert "问题" not in json.dumps(recorder.events, ensure_ascii=False)


def test_json_payload_and_schema(settings, recorder):
    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in body["messages"][0]["content"]
        assert "characters" in body["messages"][0]["content"]
        return httpx.Response(200, json=response_body(json.dumps(DEMO_JSON)))
    result = client_for(settings, recorder, handler).extract_json("提取", DemoExtraction)
    assert isinstance(result.content, DemoExtraction)
    assert result.content.characters == ["林舟", "妹妹"]


def test_configurable_model_url_and_thinking(settings, recorder):
    settings = settings.model_copy(update={
        "base_url": "https://api.deepseek.com/v1", "extraction_model": "other-model",
        "extraction_thinking": "enabled",
    })
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "other-model"
        assert body["thinking"]["type"] == "enabled"
        return httpx.Response(200, json=response_body(json.dumps(DEMO_JSON)))
    client_for(settings, recorder, handler).extract_json("提取", DemoExtraction)


@pytest.mark.parametrize("status,code", [
    (400, "invalid_request"), (401, "authentication"), (402, "balance"),
    (403, "permission"), (404, "not_found"), (422, "invalid_parameters"), (302, "http_error"),
])
def test_non_retryable_errors_never_echo_remote_body(settings, recorder, status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "sk-test-do-not-log private manuscript"})
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("private prompt")
    assert caught.value.code == code
    assert len(calls) == len(recorder.events) == 1
    assert "sk-test-do-not-log" not in str(caught.value)
    assert "private manuscript" not in str(caught.value)
    assert "private prompt" not in json.dumps(recorder.events)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_status_retries_then_succeeds(settings, recorder, status):
    calls = []
    sleeps = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, json={"error": "temporary"}, headers={"Retry-After": "1"})
        return httpx.Response(200, json=response_body())
    result = client_for(settings, recorder, handler, sleeper=sleeps.append).generate_text("你好")
    assert result.attempts == 2
    assert len(sleeps) == 1 and 1 <= sleeps[0] <= 1.1
    assert len({event["call_id"] for event in recorder.events}) == 1


def test_retry_budget_exhausted(settings, recorder):
    def handler(request):
        return httpx.Response(503, json={})
    with pytest.raises(LLMError, match="暂时不可用"):
        client_for(settings, recorder, handler).generate_text("你好")
    assert len(recorder.events) == settings.max_retries + 1


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network")])
def test_network_errors_have_bounded_retries(settings, recorder, exception, code):
    def handler(request):
        raise exception("private remote detail", request=request)
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("你好")
    assert caught.value.code == code
    assert len(recorder.events) == 3
    assert recorder.events[0]["usage"]["total_tokens"] is None
    assert "private remote detail" not in str(caught.value)


@pytest.mark.parametrize("content,code", [
    ("   ", "empty_response"), ("{", "invalid_json"),
    ('{"characters": [], "events": []}', "schema_error"),
    ('[]', "schema_error"), ('{"x": NaN}', "invalid_json"),
])
def test_bad_json_retries_and_records_billed_failed_attempt(settings, recorder, content, code):
    def handler(request):
        text = content if not recorder.events else json.dumps(DEMO_JSON)
        return httpx.Response(200, json=response_body(text))
    result = client_for(settings, recorder, handler).extract_json("提取", DemoExtraction)
    assert result.attempts == 2
    assert recorder.events[0]["status"] == code
    assert recorder.events[0]["usage"]["total_tokens"] == 28
    assert recorder.events[1]["status"] == "success"


def test_http_and_json_failures_share_one_retry_budget(settings, recorder):
    def handler(request):
        if len(recorder.events) == 0:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=response_body("{"))
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).extract_json("提取", DemoExtraction)
    assert caught.value.code == "invalid_json"
    assert len(recorder.events) == 3


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [None]}, None])
def test_malformed_response_is_classified(settings, recorder, payload):
    settings = settings.model_copy(update={"max_retries": 0})
    def handler(request):
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(200, text="bad")
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("你好")
    assert caught.value.code == "invalid_response"
    assert len(recorder.events) == 1


def test_truncated_output_is_not_retried_or_accepted(settings, recorder):
    def handler(request):
        return httpx.Response(200, json=response_body('{"partial":', finish="length"))
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).extract_json("提取", DemoExtraction)
    assert caught.value.code == "truncated"
    assert len(recorder.events) == 1
    assert recorder.events[0]["usage"]["total_tokens"] == 28


def test_missing_usage_remains_unknown(settings, recorder):
    def handler(request):
        return httpx.Response(200, json=response_body(usage=False))
    result = client_for(settings, recorder, handler).generate_text("你好")
    assert result.usage.total_tokens is None
    assert result.usage.prompt_tokens is None


def test_non_numeric_usage_is_not_logged(settings, recorder):
    def handler(request):
        payload = response_body()
        payload["usage"] = {"total_tokens": "bad", "private": "sk-test-do-not-log", "bad_count": True}
        return httpx.Response(200, json=payload)
    result = client_for(settings, recorder, handler).generate_text("你好")
    assert result.usage.total_tokens is None
    assert result.usage.numeric_details == {}


def test_log_contains_no_prompt_response_or_api_key(settings, tmp_path):
    log = tmp_path / "usage.jsonl"
    recorder = JsonlCallRecorder(log, secrets=(settings.api_key.get_secret_value(),))
    def handler(request):
        return httpx.Response(200, json=response_body("private response"))
    client_for(settings, recorder, handler).generate_text("private manuscript", system_prompt="private system")
    text = log.read_text(encoding="utf-8")
    for private in ("private response", "private manuscript", "private system", "sk-test-do-not-log"):
        assert private not in text
    assert json.loads(text)["usage"]["total_tokens"] == 28


def test_log_failure_does_not_replay_successful_request(settings, tmp_path):
    occupied = tmp_path / "not_a_directory"
    occupied.write_text("x")
    recorder = JsonlCallRecorder(occupied / "usage.jsonl")
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response_body())
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("你好")
    assert caught.value.code == "usage_log_error"
    assert len(calls) == 1


def test_empty_prompt_never_makes_a_request(settings, recorder):
    def handler(request):
        pytest.fail("empty prompt must not call HTTP")
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("  ")
    assert caught.value.code == "empty_prompt"
    assert recorder.events == []


def test_nonstop_response_not_accepted(settings, recorder):
    def handler(request):
        return httpx.Response(200, json=response_body("半截结果", finish="content_filter"))
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("你好")
    assert caught.value.code == "incomplete_response"
    assert len(recorder.events) == 1


def test_invalid_url_is_classified_without_retry(settings, recorder):
    def handler(request):
        raise httpx.InvalidURL("private detail")
    with pytest.raises(LLMError) as caught:
        client_for(settings, recorder, handler).generate_text("你好")
    assert caught.value.code == "invalid_endpoint"
    assert len(recorder.events) == 1
    assert "private detail" not in str(caught.value)


def test_http_error_usage_is_preserved(settings, recorder):
    def handler(request):
        return httpx.Response(402, json={"usage": {"prompt_tokens": 18}})
    with pytest.raises(LLMError):
        client_for(settings, recorder, handler).generate_text("你好")
    assert recorder.events[0]["usage"]["prompt_tokens"] == 18
    assert recorder.events[0]["usage"]["total_tokens"] is None


def test_large_numeric_usage_does_not_overflow(settings, recorder):
    def handler(request):
        payload = response_body()
        payload["usage"]["future_count"] = 10 ** 400
        return httpx.Response(200, json=payload)
    result = client_for(settings, recorder, handler).generate_text("你好")
    assert result.usage.numeric_details["future_count"] == 10 ** 400
