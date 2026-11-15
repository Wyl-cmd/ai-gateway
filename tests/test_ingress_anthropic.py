"""Anthropic 协议接入层（/v1/messages）纯逻辑测试。

形状来源：refs/new-api 的 relaykit/dto/claude.go、relayconvert/（Claude↔OpenAI
转换）、reasonmap（stop_reason 双向映射）——照抄成熟实现，不发明形状。
"""

from __future__ import annotations

import json

import pytest

from gateway.app import GatewayDeps
from gateway.observability.tokens import TokenMeter
from gateway.providers.openai import OpenAIAdaptor
from gateway.routing.pool import Candidate, Pool
from tests.test_ingress import (ANSWER_EVENTS, REFUSAL_EVENTS,
                                _stream_deps, client_for)


# ═══════════════════════════════════════════════════════════════
# 请求解析（Claude → 内部统一格式）
# ═══════════════════════════════════════════════════════════════
def test_解析_字符串content与顶层system():
    from gateway.ingress_anthropic import parse_messages_request

    body = {
        "model": "deepseek-chat",
        "max_tokens": 1024,
        "system": "你是一个助手",
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
            {"role": "user", "content": "继续"},
        ],
    }
    u = parse_messages_request(body)
    assert u["model"] == "deepseek-chat"
    assert u["messages"][0] == {"role": "system", "content": "你是一个助手"}
    assert u["messages"][-1] == {"role": "user", "content": "继续"}
    assert u["max_tokens"] == 1024
    assert u["stream"] is False


def test_解析_块数组content与块数组system():
    from gateway.ingress_anthropic import parse_messages_request

    body = {
        "model": "m",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "规则一"}, {"type": "text", "text": "规则二"}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "部分一"},
            {"type": "text", "text": "部分二"},
        ]}],
    }
    u = parse_messages_request(body)
    assert u["messages"][0]["content"] == "规则一\n规则二"
    assert u["messages"][-1]["content"] == "部分一\n部分二"


def test_解析_stop_sequences映射为stop():
    from gateway.ingress_anthropic import parse_messages_request

    u = parse_messages_request({"model": "m", "max_tokens": 10,
                                "stop_sequences": ["END", "STOP"],
                                "messages": [{"role": "user", "content": "x"}]})
    assert u["stop"] == ["END", "STOP"]


def test_解析_缺max_tokens_按正版报invalid_request():
    """正版 Anthropic 对缺 max_tokens 返回 invalid_request_error —— 完全一致。"""
    from gateway.ingress_anthropic import parse_messages_request

    with pytest.raises(ValueError) as e:
        parse_messages_request({"model": "m",
                                "messages": [{"role": "user", "content": "x"}]})
    assert "max_tokens" in str(e.value)


def test_解析_空messages报错():
    from gateway.ingress_anthropic import parse_messages_request

    with pytest.raises(ValueError):
        parse_messages_request({"model": "m", "max_tokens": 10, "messages": []})


def test_解析_tool_use块_本轮明确不支持():
    """诚实边界：tool 块直接报错，不静默吞掉。"""
    from gateway.ingress_anthropic import parse_messages_request

    body = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
    }]}
    with pytest.raises(ValueError) as e:
        parse_messages_request(body)
    assert "tool_use" in str(e.value)


def test_解析_透传temperature与stream():
    from gateway.ingress_anthropic import parse_messages_request

    u = parse_messages_request({"model": "m", "max_tokens": 10, "temperature": 0.5,
                                "stream": True,
                                "messages": [{"role": "user", "content": "x"}]})
    assert u["temperature"] == 0.5
    assert u["stream"] is True


# ═══════════════════════════════════════════════════════════════
# 响应转换（内部 OpenAI 壳 → Claude 壳）
# ═══════════════════════════════════════════════════════════════
def _oai_payload(content="答案", finish_reason="stop", reasoning=""):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-gw-123", "object": "chat.completion", "created": 0,
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def test_响应转换_基本壳与stop映射():
    from gateway.ingress_anthropic import to_anthropic_response

    r = to_anthropic_response(_oai_payload())
    assert r["type"] == "message" and r["role"] == "assistant"
    assert r["model"] == "deepseek-chat"
    assert r["content"] == [{"type": "text", "text": "答案"}]
    assert r["stop_reason"] == "end_turn"        # stop → end_turn（reasonmap）
    assert r["stop_sequence"] is None
    assert r["usage"] == {"input_tokens": 11, "output_tokens": 7}


def test_响应转换_length映射max_tokens():
    from gateway.ingress_anthropic import to_anthropic_response

    r = to_anthropic_response(_oai_payload(finish_reason="length"))
    assert r["stop_reason"] == "max_tokens"


def test_响应转换_带thinking块():
    """reasoning → thinking block（与 new-api 的 ResponseOpenAI2Claude 一致）。"""
    from gateway.ingress_anthropic import to_anthropic_response

    r = to_anthropic_response(_oai_payload(reasoning="我想了一下"))
    assert r["content"][0] == {"type": "thinking", "thinking": "我想了一下"}
    assert r["content"][1] == {"type": "text", "text": "答案"}


# ═══════════════════════════════════════════════════════════════
# 流式事件序（照 new-api 的 StreamResponseOpenAI2Claude）
# ═══════════════════════════════════════════════════════════════
def test_流式事件序_完整序列():
    from gateway.ingress_anthropic import stream_events

    events = list(stream_events(
        deltas=["你", "好"],
        model="deepseek-chat", msg_id="chatcmpl-gw-1",
        finish_reason="stop",
        usage={"prompt_tokens": 11, "completion_tokens": 7},
    ))
    types = [json.loads(d)["type"] for _ev, d in events]

    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]

    # 每帧都是 (event 名, data) 成对，event 名与 data.type 一致
    assert all(ev == json.loads(d)["type"] for ev, d in events)

    start = json.loads(events[0][1])
    assert start["message"]["role"] == "assistant"
    assert start["message"]["type"] == "message"
    assert start["message"]["usage"]["input_tokens"] == 11

    d0 = json.loads(events[2][1])
    assert d0["delta"] == {"type": "text_delta", "text": "你"}
    assert d0["index"] == 0

    md = json.loads(events[5][1])
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["output_tokens"] == 7

    assert json.loads(events[6][1]) == {"type": "message_stop"}


def test_流式事件序_无usage时占位():
    """上游没给 usage → input_tokens/output_tokens 用 0 占位，字段不缺。"""
    from gateway.ingress_anthropic import stream_events

    events = list(stream_events(deltas=["x"], model="m", msg_id="i",
                                finish_reason="length", usage=None))
    start = json.loads(events[0][1])
    assert start["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}
    md = json.loads(events[4][1])
    assert md["delta"]["stop_reason"] == "max_tokens"


# ═══════════════════════════════════════════════════════════════
# 错误壳
# ═══════════════════════════════════════════════════════════════
def test_错误壳_状态码映射():
    from gateway.ingress_anthropic import error_body

    assert error_body(401, "no key")["error"]["type"] == "authentication_error"
    assert error_body(400, "bad")["error"]["type"] == "invalid_request_error"
    assert error_body(503, "down")["error"]["type"] == "api_error"
    assert error_body(401, "x")["type"] == "error"


# ═══════════════════════════════════════════════════════════════
# ⑪ /v1/messages —— Anthropic 协议接入（形状对照正版）
# ═══════════════════════════════════════════════════════════════
def _anthropic_body(**over):
    b = {"model": "gpt-4o", "max_tokens": 100, "stream": False,
         "messages": [{"role": "user", "content": "你好"}]}
    b.update(over)
    return b


@pytest.mark.asyncio
async def test_v1_messages_非流式_正版message壳():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    async with client_for(deps) as c:
        r = await c.post("/v1/messages", json=_anthropic_body(),
                         headers={"x-api-key": "test-key",
                                  "anthropic-version": "2023-06-01"})
    assert r.status_code == 200, getattr(r, "text", "")[:300]
    j = r.json()
    assert j["type"] == "message" and j["role"] == "assistant"
    assert j["content"] == [{"type": "text", "text": "答案"}]
    assert j["stop_reason"] == "end_turn" and j["stop_sequence"] is None
    assert j["usage"] == {"input_tokens": 10, "output_tokens": 5}


@pytest.mark.asyncio
async def test_v1_messages_双鉴权_x_api_key与bearer等价():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    async with client_for(deps) as c:
        r1 = await c.post("/v1/messages", json=_anthropic_body(),
                          headers={"x-api-key": "test-key"})
        r2 = await c.post("/v1/messages", json=_anthropic_body(),
                          headers={"Authorization": "Bearer test-key"})
        r3 = await c.post("/v1/messages", json=_anthropic_body(),
                          headers={"x-api-key": "wrong"})
    assert r1.status_code == r2.status_code == 200
    assert r3.status_code == 401
    assert r3.json()["error"]["type"] == "authentication_error"   # Claude 错误壳


@pytest.mark.asyncio
async def test_v1_messages_缺max_tokens_400正版语义():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    b = _anthropic_body()
    del b["max_tokens"]
    async with client_for(deps) as c:
        r = await c.post("/v1/messages", json=b,
                         headers={"x-api-key": "test-key"})
    assert r.status_code == 400
    j = r.json()
    assert j["type"] == "error"
    assert j["error"]["type"] == "invalid_request_error"
    assert "max_tokens" in j["error"]["message"]


@pytest.mark.asyncio
async def test_v1_messages_tool_use块_明确报错():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    b = _anthropic_body(messages=[{"role": "user", "content": [
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]}])
    async with client_for(deps) as c:
        r = await c.post("/v1/messages", json=b,
                         headers={"x-api-key": "test-key"})
    assert r.status_code == 400
    assert "tool_use" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_v1_messages_流式_事件序与破甲重试():
    """Anthropic 协议的流式同样走破甲阶梯；事件序与正版一致。"""
    deps, calls = _stream_deps(
        lambda i, u: REFUSAL_EVENTS if i == 1 else ANSWER_EVENTS)
    async with client_for(deps) as c:
        r = await c.post("/v1/messages",
                         json=_anthropic_body(stream=True),
                         headers={"x-api-key": "test-key"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")

    # 解析 event:/data: 双行帧
    events = []
    for block in r.text.split("\n\n"):
        ev, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                ev = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if ev and data:
            events.append((ev, json.loads(data)))

    types = [e["type"] for _ev, e in events]
    assert types == ["message_start", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    # 破甲在 Anthropic 协议下同样生效：上游第二次调用收到变形 prompt
    assert calls[0]["messages"][-1]["content"] == "你好"
    assert calls[1]["messages"][-1]["content"] != "你好"
    assert r.headers["x-gateway-jailbreak-level"] == "1"
    text = "".join(e["delta"]["text"] for t, e in events
                   if t == "content_block_delta")
    assert text == "答案"
    md = [e for t, e in events if t == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["output_tokens"] == 5


@pytest.mark.asyncio
async def test_v1_messages_非流式破甲升级():
    """非流式在 Anthropic 协议下同样走破甲 + 响应头带真实 level。"""
    deps, _ = _stream_deps(
        lambda i, u: REFUSAL_EVENTS if i == 1 else ANSWER_EVENTS)
    async with client_for(deps) as c:
        r = await c.post("/v1/messages", json=_anthropic_body(),
                         headers={"x-api-key": "test-key"})
    assert r.status_code == 200
    assert r.headers["x-gateway-jailbreak-level"] == "1"
    assert r.json()["content"][0]["text"] == "答案"
