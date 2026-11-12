"""G2 转发层测试 —— 全部走 httpx.MockTransport，不发真请求。

门② 判据：这批测试在实现之前必须「红」，且红的因由是
ImportError / AssertionError（功能没实现），不是 NameError（骨架没建好）。
"""

from __future__ import annotations

import json

import httpx
import pytest

# ── 门② 时这些 import 会失败（模块不存在）——这是预期 ──
from gateway.providers.base import (
    Adaptor,
    ProviderError,
    classify_failure,
    is_health_failure,
    is_quota_exhausted,
)
from gateway.providers.openai import OpenAIAdaptor
from gateway.providers.anthropic import AnthropicAdaptor
from gateway.providers.deepseek import DeepSeekAdaptor
from gateway.providers.gemini import GeminiAdaptor
from gateway.providers.aggregator import Aggregator


# ═══════════════════════════════════════════════════════════════
# 统一请求格式（网关内部格式，各家都要能从这里转出去）
# ═══════════════════════════════════════════════════════════════
def unified_request():
    return {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ],
        "temperature": 0.7,
        "max_tokens": 128,
        "stream": False,
    }


# ═══════════════════════════════════════════════════════════════
# ① transform_request：统一格式 → 各家格式
# ═══════════════════════════════════════════════════════════════
def test_openai_transform_request_保持原样():
    """OpenAI 是基准格式，转出去应该几乎不变。"""
    out = OpenAIAdaptor().transform_request(unified_request())
    assert out["model"] == "gpt-4o"
    assert out["messages"] == unified_request()["messages"]
    assert out["temperature"] == 0.7
    assert out["max_tokens"] == 128


def test_anthropic_transform_request_搬走_system_字段():
    """Anthropic 的 system 不在 messages 里，是顶层独立字段；max_tokens 必填。"""
    out = AnthropicAdaptor().transform_request(unified_request())
    # system 被抽走
    assert out["system"] == "你是助手"
    assert not any(m.get("role") == "system" for m in out["messages"])
    # 剩下的 user 消息还在
    assert out["messages"] == [{"role": "user", "content": "你好"}]
    # max_tokens 必填，且叫 max_tokens（不是 max_completion_tokens）
    assert out["max_tokens"] == 128


def test_gemini_transform_request_换掉_messages_结构():
    """Gemini 用 contents + parts，role 名也不同（assistant→model, user→user）。"""
    out = GeminiAdaptor().transform_request(unified_request())
    assert "contents" in out
    assert "messages" not in out
    # system 走 systemInstruction
    assert "systemInstruction" in out
    # 用户消息变成 parts
    assert out["contents"][0]["parts"][0]["text"] == "你好"
    assert out["contents"][0]["role"] == "user"
    # 生成参数收进 generationConfig
    assert out["generationConfig"]["temperature"] == 0.7
    assert out["generationConfig"]["maxOutputTokens"] == 128


def test_deepseek_transform_request_复用_openai_格式():
    """DeepSeek 走 OpenAI 兼容协议，字段应该和 OpenAI 一致。"""
    a = OpenAIAdaptor().transform_request(unified_request())
    d = DeepSeekAdaptor().transform_request(unified_request())
    assert d["messages"] == a["messages"]
    assert d["temperature"] == a["temperature"]


# ═══════════════════════════════════════════════════════════════
# ② transform_response：各家格式 → 统一格式
# ═══════════════════════════════════════════════════════════════
def test_openai_transform_response_取第一个_choice():
    raw = {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好呀"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    out = OpenAIAdaptor().transform_response(raw)
    assert out["content"] == "你好呀"
    assert out["finish_reason"] == "stop"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5


def test_openai_解析思考模型的_reasoning_content():
    """思考模型（deepseek-flash / o1 / qwq）把思考放在 reasoning_content，
    content 只有最终答案。不取它会导致 max_tokens 小时 content 为空，
    被上层误判成「拒绝」然后无意义重试。
    """
    raw = {
        "choices": [{
            "message": {"role": "assistant", "content": "",
                        "reasoning_content": "用户想问的是…"},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 34, "completion_tokens": 20, "total_tokens": 54,
                  "completion_tokens_details": {"reasoning_tokens": 20}},
    }
    out = OpenAIAdaptor().transform_response(raw)
    assert out["reasoning"] == "用户想问的是…"
    assert out["content"] == ""
    assert out["finish_reason"] == "length"
    # usage 必须完整，否则对账对不上
    assert out["usage"]["prompt_tokens"] == 34
    assert out["usage"]["reasoning_tokens"] == 20


def test_普通模型没有_reasoning_字段时为空串():
    raw = {"choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
           "usage": {}}
    out = OpenAIAdaptor().transform_response(raw)
    assert out["reasoning"] == ""


def test_anthropic_transform_response_拼接_content_块():
    """Anthropic 的 content 是 block 数组，要拼成一段文本。"""
    raw = {
        "id": "msg_1",
        "content": [{"type": "text", "text": "你好"},
                    {"type": "text", "text": "呀"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    out = AnthropicAdaptor().transform_response(raw)
    assert out["content"] == "你好呀"
    assert out["finish_reason"] == "end_turn"
    # usage 字段名对齐到统一格式
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5


def test_gemini_transform_response_读_candidates():
    raw = {
        "candidates": [{"content": {"parts": [{"text": "你好呀"}], "role": "model"},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    out = GeminiAdaptor().transform_response(raw)
    assert out["content"] == "你好呀"
    assert out["finish_reason"] == "STOP"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 5


# ═══════════════════════════════════════════════════════════════
# ③ SSE 流式解析：逐块喂，边界要被正确切分
# ═══════════════════════════════════════════════════════════════
def test_parse_sse_处理被切断的_chunk():
    """一块被切成两半的 SSE 数据必须能拼回来 —— 这是流式最常见的坑。"""
    a = OpenAIAdaptor()
    # 第一个 chunk 只到一半，第二个 chunk 补齐
    part1 = b'data: {"choices":[{"delta":{"content":"he'
    part2 = b'llo"}}]}\n\ndata: [DONE]\n\n'
    got = a.parse_sse(part1) + a.parse_sse(part2)
    # 第一块解不出来（不完整），第二块把剩下的拼上后才产出
    texts = [d.get("delta", "") for d in got if d.get("delta")]
    assert "hello" in "".join(texts)


def test_parse_sse_遇到_DONE_就结束():
    a = OpenAIAdaptor()
    out = a.parse_sse(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n')
    assert len(out) >= 1
    # [DONE] 不该被当成 JSON 解析错误抛出
    assert all(isinstance(d, dict) for d in out)


def test_parse_sse_忽略空行和注释():
    a = OpenAIAdaptor()
    out = a.parse_sse(b': ping\n\ndata: {"choices":[{"delta":{"content":"x"}}]}\n\n')
    assert len(out) == 1
    assert out[0]["delta"] == "x"


# ═══════════════════════════════════════════════════════════════
# ④ 失败分类：G3 路由层的唯一输入
# ═══════════════════════════════════════════════════════════════
def test_429_算健康故障():
    e = ProviderError("rate limited", status=429)
    assert is_health_failure(e) is True


def test_500_算健康故障():
    assert is_health_failure(ProviderError("boom", status=500)) is True
    assert is_health_failure(ProviderError("boom", status=503)) is True


def test_408_算健康故障():
    assert is_health_failure(ProviderError("timeout", status=408)) is True


def test_400_400_401_402_404_不算健康故障():
    """关键：客户端错误不该毒化路由 —— 否则一个坏请求会把整家 provider 打成故障。"""
    for code in (400, 401, 403, 402, 404):
        assert is_health_failure(ProviderError("x", status=code)) is False, f"{code} 不该算健康故障"


def test_网络异常算健康故障():
    assert is_health_failure(httpx.ConnectError("refused")) is True


def test_402_带配额文案算配额耗尽():
    e = ProviderError("You have insufficient credits", status=402)
    assert is_quota_exhausted(e) is True


def test_429_带配额文案算配额耗尽():
    e = ProviderError("quota exhausted for this key", status=429)
    assert is_quota_exhausted(e) is True


def test_429_不带配额文案不算配额耗尽():
    """429 分两种：限速（等一下还能用）vs 配额耗尽（这个号废了）。不能混为一谈。"""
    e = ProviderError("rate limit exceeded, retry after 60s", status=429)
    assert is_quota_exhausted(e) is False


def test_配额耗尽标记独立于健康标记():
    """配额耗尽 → 只跳过该账号；健康故障 → 跳过整个 provider。
    两个标记必须能区分，这是三层降级的基础。"""
    e = ProviderError("insufficient credits", status=402)
    assert e.is_quota_exhausted is True
    assert e.is_health_failure is False   # 402 不算健康故障


def test_classify_failure_给出唯一归类():
    assert classify_failure(ProviderError("x", status=429)) == "health"
    assert classify_failure(ProviderError("quota exhausted", status=429)) == "quota"
    assert classify_failure(ProviderError("bad", status=400)) == "client"


# ═══════════════════════════════════════════════════════════════
# ⑤ Aggregator：多 provider 注册与选择（不发请求）
# ═══════════════════════════════════════════════════════════════
def test_aggregator_按名字取_adaptor():
    agg = Aggregator()
    agg.register("openai", OpenAIAdaptor())
    agg.register("anthropic", AnthropicAdaptor())
    assert isinstance(agg.get("openai"), OpenAIAdaptor)
    assert isinstance(agg.get("anthropic"), AnthropicAdaptor)


def test_aggregator_未知名字抛错():
    agg = Aggregator()
    with pytest.raises(KeyError):
        agg.get("nope")


def test_aggregator_列出已注册的():
    agg = Aggregator()
    agg.register("openai", OpenAIAdaptor())
    agg.register("deepseek", DeepSeekAdaptor())
    assert set(agg.names()) == {"openai", "deepseek"}


# ═══════════════════════════════════════════════════════════════
# ⑥ Adaptor 契约完整性
# ═══════════════════════════════════════════════════════════════
def test_四个_adaptor_都实现了契约():
    for cls in (OpenAIAdaptor, AnthropicAdaptor, DeepSeekAdaptor, GeminiAdaptor):
        a = cls()
        for m in ("transform_request", "transform_response", "parse_sse", "chat", "stream"):
            assert callable(getattr(a, m, None)), f"{cls.__name__} 缺 {m}"


def test_adaptor_子类必须声明_name():
    """每家 provider 要能自报家门（用于 trace 和错误归因）。"""
    assert OpenAIAdaptor().name == "openai"
    assert AnthropicAdaptor().name == "anthropic"
    assert DeepSeekAdaptor().name == "deepseek"
    assert GeminiAdaptor().name == "gemini"
