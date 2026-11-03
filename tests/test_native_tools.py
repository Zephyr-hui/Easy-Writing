import json

import httpx
import pytest

from easy_writing.llm.base import LLMError
from easy_writing.llm.deepseek import DeepSeekClient


TOOLS = [{"type": "function", "function": {"name": "read_source", "description": "读取原文",
           "parameters": {"type": "object", "properties": {"keys": {"type": "array", "items": {"type": "string"}}}, "required": ["keys"]}}}]


def test_native_tool_wire_format_and_continuation(settings, recorder):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["thinking"] == {"type": "disabled"}
        assert body["tools"] == TOOLS
        assert "response_format" not in body
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": "native-1", "type": "function", "function": {
                    "name": "read_source", "arguments": '{"keys":["fact:1"]}'}}]}}], "usage": {"total_tokens": 30}})
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["tool_call_id"] == "native-1"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "完成"}}]})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = DeepSeekClient(settings, recorder, http_client=http)
    messages = [{"role": "user", "content": "查询"}]
    first = client.call_tools(messages, TOOLS)
    call = first.content.tool_calls[0]
    assert call.name == "read_source" and call.arguments == {"keys": ["fact:1"]}
    messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)}}]},
                 {"role": "tool", "tool_call_id": call.id, "content": "局部原文"}]
    assert client.call_tools(messages, TOOLS).content.text == "完成"
    assert len(recorder.events) == 2 and recorder.events[0]["mode"] == "tools"


def test_native_tool_calls_have_no_hidden_network_retries(settings, recorder):
    def handler(request):
        return httpx.Response(503, json={"error": "private"})
    client = DeepSeekClient(settings, recorder, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError):
        client.call_tools([{"role": "user", "content": "查询"}], TOOLS)
    assert len(recorder.events) == 1


@pytest.mark.parametrize("arguments", ["[]", "not json", '{"x":NaN}'])
def test_native_invalid_tool_arguments_are_not_executed(settings, recorder, arguments):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
            "tool_calls": [{"id": "x", "type": "function", "function": {"name": "read_source", "arguments": arguments}}]}}]})
    client = DeepSeekClient(settings, recorder, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError) as caught:
        client.call_tools([{"role": "user", "content": "查询"}], TOOLS)
    assert caught.value.code == "invalid_tool_response"


def test_native_error_preserves_known_usage_and_call_id(settings, recorder):
    def handler(request):
        return httpx.Response(503, json={"usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}})
    client = DeepSeekClient(settings, recorder, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(LLMError) as caught:
        client.call_tools([{"role": "user", "content": "查询"}], TOOLS)
    assert caught.value.usage.total_tokens == 22
    assert caught.value.call_id == recorder.events[0]["call_id"]
