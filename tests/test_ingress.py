"""G8 接入层测试 —— 第一次把零件装成机器。

全链路：鉴权 → 破甲 → 路由 → 转发 → 拒绝检测 → 升级重试

**不发真请求**：上游用 `call_fn` 注入桩函数。这样测试跑得快、无网络依赖、
还能精确控制「第一次返回拒绝话术」这种场景。

## 接入层唯一该做的事

把前面几层的输出接起来，不做任何自己独立的判断。
一旦接入层开始「顺手也判一下」，它就成了第二个决策点，
两处判断必然会不一致 —— 那时候「为什么走了这条路」就再也说不清了。
"""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from gateway.app import create_app, GatewayDeps
from gateway.observability.tokens import TokenMeter
from gateway.providers.base import ProviderError
from gateway.providers.openai import OpenAIAdaptor
from gateway.routing.pool import Candidate, Pool


# ═══════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════
def make_deps(responder=None, *, jailbreak=True, api_key="test-key"):
    """构造依赖。responder(call_index, unified) -> dict 控制上游返回。"""
    calls = []

    def default_responder(i, unified):
        return {"content": "这是正常回答", "finish_reason": "stop",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    resp = responder or default_responder

    def call_fn(adaptor, unified):
        out = resp(len(calls), unified)
        calls.append(out)
        if isinstance(out, Exception):
            raise out
        return out

    pool = Pool()
    cands = [Candidate(provider="fake", account="k1", model="gpt-4o")]
    deps = GatewayDeps(
        pool=pool,
        candidates=cands,
        adaptors={("fake", "gpt-4o"): OpenAIAdaptor("http://up", "sk-x")},
        call_fn=call_fn,
        meter=TokenMeter(),
        api_key=api_key,
        jailbreak_enabled=jailbreak,
    )
    return deps, calls


def client_for(deps):
    app = create_app(deps)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://gw")


def body(model="gpt-4o", stream=False, content="你好"):
    return {"model": model, "messages": [{"role": "user", "content": content}],
            "stream": stream}


HDR = {"Authorization": "Bearer test-key"}


# ═══════════════════════════════════════════════════════════════
# ① 正常链路
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_合法请求_200_且带_trace_头():
    deps, calls = make_deps()
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "这是正常回答"
    assert "X-Gateway-Trace" in r.headers
    # 四件套要在 trace 头里
    tr = r.headers["X-Gateway-Trace"]
    assert "mode=" in tr and "requested=" in tr and "actual=" in tr


@pytest.mark.asyncio
async def test_健康检查不需要鉴权():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ═══════════════════════════════════════════════════════════════
# ② 鉴权
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_缺_key_返回_401():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body())
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_错_key_返回_401():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(),
                         headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_鉴权失败不调用上游():
    deps, calls = make_deps()
    async with client_for(deps) as c:
        await c.post("/v1/chat/completions", json=body(),
                     headers={"Authorization": "Bearer wrong"})
    assert calls == [], "没通过鉴权就不该碰上游"


# ═══════════════════════════════════════════════════════════════
# ③ 破甲：被拒后自动升级
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_破甲被拒后自动升级到_L1():
    """第 0 次原样发 → 上游拒绝；第 1 次变形后再发 → 成功。
    最终必须返回成功内容，而不是把拒绝话术透给用户。"""
    def responder(i, unified):
        if i == 0:
            return {"content": "I'm sorry, but I can't help with that.",
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13}}
        return {"content": "变形后的正确答案", "finish_reason": "stop",
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}

    deps, calls = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "变形后的正确答案"
    assert len(calls) >= 2, "被拒后必须再试一次"
    assert "X-Gateway-Jailbreak-Level" in r.headers


@pytest.mark.asyncio
async def test_关掉破甲时不重试():
    """X-Gateway-Jailbreak: off 时，拒绝就照实返回 —— 用户明确说了不要破甲。"""
    def responder(i, unified):
        return {"content": "I'm sorry, but I can't help with that.",
                "finish_reason": "stop", "usage": {}}

    deps, calls = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(),
                         headers={**HDR, "X-Gateway-Jailbreak": "off"})

    assert r.status_code == 200
    assert "can't help" in r.json()["choices"][0]["message"]["content"]
    assert len(calls) == 1, "关掉破甲就不该重试"


@pytest.mark.asyncio
async def test_升级次数有上限():
    """上游一直拒绝时不能无限重试 —— 必须有上限，否则烧钱。"""
    def responder(i, unified):
        return {"content": "I'm sorry, but I can't help with that.",
                "finish_reason": "stop", "usage": {}}

    deps, calls = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)
    assert len(calls) <= 6, f"重试了 {len(calls)} 次，超上限"
    assert r.status_code == 200, "全都拒绝也要返回最后一次的结果，不能 500"


# ═══════════════════════════════════════════════════════════════
# ④ 路由失败
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_上游全挂返回_502():
    def responder(i, unified):
        raise ProviderError("server error", status=503, provider="fake")

    deps, _ = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "upstream_error"


@pytest.mark.asyncio
async def test_配额耗尽会换号():
    """配额耗尽只跳账号 —— 同 provider 其他 key 还能用。"""
    calls = []

    def responder(i, unified):
        if i == 0:
            raise ProviderError("insufficient credits", status=402, provider="fake")
        return {"content": "换了账号后成功", "finish_reason": "stop",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    def call_fn(adaptor, unified):
        # 先计数再调 responder —— responder 内部可能 raise，
        # 放在后面会导致 calls 永远不增长，每次都走「第一次」分支，
        # 表现成「换号逻辑坏了」，实际是桩的问题。
        i = len(calls)
        calls.append(i)
        out = responder(i, unified)
        if isinstance(out, Exception):
            raise out
        return out

    pool = Pool()
    cands = [Candidate(provider="fake", account="k1", model="gpt-4o"),
             Candidate(provider="fake", account="k2", model="gpt-4o")]
    deps = GatewayDeps(
        pool=pool, candidates=cands,
        adaptors={("fake", "gpt-4o"): OpenAIAdaptor("http://up", "sk")},
        call_fn=call_fn, meter=TokenMeter(), api_key="test-key",
    )
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "换了账号后成功"


# ═══════════════════════════════════════════════════════════════
# ⑤ 流式
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_流式逐块透传():
    """流式必须真的分多块到 —— 一次性吐完等于没流式。"""
    def stream_fn(adaptor, unified):
        for piece in ("你", "好", "呀"):
            yield {"delta": piece, "finish_reason": None}
        yield {"delta": "", "finish_reason": "stop"}

    pool = Pool()
    cands = [Candidate(provider="fake", account="k1", model="gpt-4o")]
    deps = GatewayDeps(
        pool=pool, candidates=cands,
        adaptors={("fake", "gpt-4o"): OpenAIAdaptor("http://up", "sk")},
        call_fn=None, stream_fn=stream_fn, meter=TokenMeter(), api_key="test-key",
    )
    async with client_for(deps) as c:
        async with c.stream("POST", "/v1/chat/completions",
                            json=body(stream=True), headers=HDR) as r:
            assert r.status_code == 200
            text = "".join([chunk async for chunk in r.aiter_text()])

    assert "data: " in text
    assert "你" in text and "好" in text
    assert "[DONE]" in text


# ═══════════════════════════════════════════════════════════════
# ⑤a AUTO 模式必须型号亲和（真跑抓到的 bug）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_auto_模式不会跨型号返回():
    """请求 gpt-4o 就不能给 deepseek-chat —— 除非显式开了 any_model。"""
    def responder(i, unified):
        return {"content": "答案", "finish_reason": "stop", "usage": {}}

    pool = Pool()
    cands = [Candidate(provider="deepseek", account="k1", model="deepseek-chat"),
             Candidate(provider="openai", account="k1", model="gpt-4o"),
             Candidate(provider="openai", account="k2", model="gpt-4o")]
    deps = GatewayDeps(
        pool=pool, candidates=cands,
        adaptors={("openai", "gpt-4o"): OpenAIAdaptor("http://up", "sk"),
                  ("deepseek", "deepseek-chat"): OpenAIAdaptor("http://up", "sk")},
        call_fn=lambda a, u: responder(0, u), meter=TokenMeter(),
        api_key="test-key",          # 必须和 HDR 里的 Bearer 一致
        mode="auto", policy="strict",
    )
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions",
                         json=body(model="gpt-4o"), headers=HDR)

    assert r.status_code == 200
    tr = r.headers["X-Gateway-Trace"]
    assert "actual=gpt-4o" in tr, f"跨型号了: {tr}"
    assert "X-Gateway-Route-Warning" not in r.headers, "同型号不该有降级告警"


# ═══════════════════════════════════════════════════════════════
# ⑤b 中文内容不能炸响应头（真跑抓到的 bug）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_中文模型名和中文报错不能把请求打成_500():
    """开了 trace_json_header 后，trace 里的中文（模型名/候选原因/报错）
    会全进响应头。HTTP 头只能 latin-1，处理不当就 UnicodeEncodeError → 500。
    这条守的就是真跑时踩到的那个坑。"""
    def responder(i, unified):
        if i == 0:
            return {"content": "I'm sorry, but I can't help with that.",
                    "finish_reason": "stop", "usage": {}}
        return {"content": "中文答案", "finish_reason": "stop", "usage": {}}

    deps, _ = make_deps(responder)
    deps.trace_json_header = True
    # model 必须和候选对上（gpt-4o），中文只出现在**内容**里 ——
    # 否则会被型号亲和筛掉变成 503，测的就不是头编码问题了
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions",
                         json=body(model="gpt-4o", content="中文问题，请详细回答"),
                         headers=HDR)
    assert r.status_code == 200, f"被打成 {r.status_code}: {r.text[:200]}"
    assert r.headers["X-Gateway-Trace-Json"].isascii()
    # 中文内容本身要能正常返回
    assert "中文" in r.json()["choices"][0]["message"]["content"]


# ═══════════════════════════════════════════════════════════════
# ⑥ token 计量接线
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_请求后记录了_token():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        await c.post("/v1/chat/completions", json=body(), headers=HDR)
    recs = deps.meter.records()
    assert len(recs) == 1
    assert recs[0].prompt_tokens == 10
    assert recs[0].completion_tokens == 5


# ═══════════════════════════════════════════════════════════════
# ⑦ 请求校验
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_缺_messages_返回_400():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json={"model": "gpt-4o"}, headers=HDR)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_空_messages_返回_400():
    deps, _ = make_deps()
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions",
                         json={"model": "gpt-4o", "messages": []}, headers=HDR)
    assert r.status_code == 400
