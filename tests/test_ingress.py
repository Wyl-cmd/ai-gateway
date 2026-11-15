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
# ④a 不给 max_tokens 时要给足默认值（思考模型必需）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_没给_max_tokens_时不注入_留给上游默认():
    """网关是管道，不替调用方决定输出预算：请求里没带 max_tokens
    就不向上游发送该字段，行为与直连上游 API 完全一致。
    （思考模型截断是上游默认值的事，网关只如实标注截断，不代为放大。）"""
    seen = {}

    def responder(i, unified):
        seen["has_max_tokens"] = "max_tokens" in unified
        return {"content": "答案", "finish_reason": "stop", "usage": {}}

    deps, _ = make_deps(responder)
    async with client_for(deps) as c:
        # 故意不给 max_tokens
        r = await c.post("/v1/chat/completions",
                         json={"model": "gpt-4o",
                               "messages": [{"role": "user", "content": "hi"}]},
                         headers=HDR)
    assert r.status_code == 200
    assert seen["has_max_tokens"] is False, "网关不该替调用方注入 max_tokens"


@pytest.mark.asyncio
async def test_给了_max_tokens_时尊重调用方():
    seen = {}

    def responder(i, unified):
        seen["max_tokens"] = unified.get("max_tokens")
        return {"content": "答案", "finish_reason": "stop", "usage": {}}

    deps, _ = make_deps(responder)
    async with client_for(deps) as c:
        await c.post("/v1/chat/completions",
                     json={"model": "gpt-4o", "max_tokens": 50,
                           "messages": [{"role": "user", "content": "hi"}]},
                     headers=HDR)
    assert seen["max_tokens"] == 50, "调用方明确给了就该用它的"


# ═══════════════════════════════════════════════════════════════
# ④b 被 max_tokens 截断 ≠ 拒绝（真跑抓到的 bug）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_被截断的思考模型不触发无意义重试():
    """实测踩到：deepseek-flash 是思考模型，max_tokens=20 全花在
    reasoning_content 上，content 为空 → detect 判「空回答=拒绝」→ 重试 6 次，
    每次都空，白烧 6 次请求还返回 0 usage。

    finish_reason=length 说明是**长度不够**，重试同一个 max_tokens 毫无意义。
    """
    def responder(i, unified):
        return {"content": "", "reasoning": "想了很久",
                "finish_reason": "length", "usage": {"prompt_tokens": 34,
                                                     "completion_tokens": 20,
                                                     "total_tokens": 54}}

    deps, calls = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert len(calls) == 1, f"被截断时重试了 {len(calls)} 次（应该 1 次）"
    assert r.json()["usage"]["prompt_tokens"] == 34, "usage 不能丢"
    # reasoning 要透出来
    assert r.json()["choices"][0]["message"].get("reasoning_content") == "想了很久"


@pytest.mark.asyncio
async def test_真空回答仍然算拒绝要重试():
    """和上一条区分：finish_reason=stop 但 content 空 —— 这是真的被拒，
    必须重试。不能因为修了截断问题就把真拒绝也放过。"""
    def responder(i, unified):
        if i == 0:
            return {"content": "", "finish_reason": "stop", "usage": {}}
        return {"content": "第二次拿到答案", "finish_reason": "stop", "usage": {}}

    deps, calls = make_deps(responder)
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert len(calls) >= 2, "真空回答该重试"
    assert r.json()["choices"][0]["message"]["content"] == "第二次拿到答案"


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
# ⑤c J5 技术有效性记忆接线（不能是死代码）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_记忆接入后每次尝试都写进_attempt_log(tmp_path):
    """J5 存在的意义是「学习」，而学习的前提是有人记战绩。
    这条守的就是接线 —— 没有它 TechniqueMemory 永远是死代码。"""
    from gateway.jailbreak.memory import TechniqueMemory
    from gateway.storage.repo import AttemptLogRepo, init_db

    db = str(tmp_path / "m.db")
    init_db(db)
    repo = AttemptLogRepo(db)

    def responder(i, unified):
        if i < 2:
            return {"content": "I'm sorry, but I can't help with that.",
                    "finish_reason": "stop", "usage": {}}
        return {"content": "成了", "finish_reason": "stop", "usage": {}}

    deps, _ = make_deps(responder)
    deps.memory = TechniqueMemory(repo)

    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert repo.count() >= 2, f"只记了 {repo.count()} 条，接线断了"


@pytest.mark.asyncio
async def test_学到的技术会被优先使用(tmp_path):
    """给某个技术灌够 5 条成功样本后，它应该在阶梯里排到最前面。"""
    from gateway.jailbreak.memory import TechniqueMemory
    from gateway.storage.repo import AttemptLogRepo, init_db

    db = str(tmp_path / "m2.db")
    init_db(db)
    repo = AttemptLogRepo(db)
    mem = TechniqueMemory(repo)

    # 让 reverse 攒够 5 条成功（先验里它排 base64 之后）
    for i in range(5):
        mem.record_attempt(request_id=f"pre{i}", model="gpt-4o", level=1,
                           technique="reverse", ok=True)

    ranked = mem.rank(model="gpt-4o", level=1)
    assert ranked[0].name == "reverse", \
        f"学到的技术该排第一，实际: {[t.name for t in ranked[:3]]}"


# ═══════════════════════════════════════════════════════════════
# ⑤d 破甲产物库接线（跨模型迁移，Master 要求的「迭代式破甲」）
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_破甲成功时把_prompt_存进产物库(tmp_path):
    """「把破甲结果和模型对应并固化下来」—— 没有这一步，产物库是死的。"""
    from gateway.jailbreak.artifact import ArtifactStore
    from gateway.storage.repo import init_db

    db = str(tmp_path / "a.db")
    init_db(db)
    store = ArtifactStore(db)

    def responder(i, unified):
        if i == 0:
            return {"content": "I'm sorry, but I can't help with that.",
                    "finish_reason": "stop", "usage": {}}
        return {"content": "成功了", "finish_reason": "stop",
                "usage": {"total_tokens": 42}}

    deps, _ = make_deps(responder)
    deps.artifacts = store

    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    items = store.all()
    assert len(items) == 1, f"产物库空了，接线断了（{len(items)} 条）"
    assert items[0].successes == 1
    assert items[0].tokens == 42
    # 存的是**变形后**发出去的 prompt，不是原问题
    assert items[0].prompt_text != "你好"


@pytest.mark.asyncio
async def test_别的模型成功过的_prompt_会被迁移过来(tmp_path):
    """★ Master 的核心需求：新模型第一次遇到同类问题，直接搬旧模型的成功 prompt。"""
    from gateway.jailbreak.artifact import ArtifactStore
    from gateway.storage.repo import init_db

    db = str(tmp_path / "b.db")
    init_db(db)
    store = ArtifactStore(db)
    # 老模型 modelA 在同类问题上成功过
    store.record_success(model="modelA", problem="你好",
                         technique="novel", prompt_text="【迁移来的】旧模型用的那句话",
                         tokens=10)

    sent = []

    def responder(i, unified):
        sent.append(unified["messages"][-1]["content"])
        if len(sent) == 1:
            return {"content": "I'm sorry, but I can't help with that.",
                    "finish_reason": "stop", "usage": {}}
        return {"content": "拿到了", "finish_reason": "stop", "usage": {}}

    deps, _ = make_deps(responder)
    deps.artifacts = store
    deps.candidates = [Candidate(provider="fake", account="k1", model="gpt-4o")]
    deps.adaptors = {("fake", "gpt-4o"): OpenAIAdaptor("http://up", "sk")}

    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)

    assert r.status_code == 200
    assert len(sent) >= 2, "该重试"
    assert "【迁移来的】旧模型用的那句话" in sent[1], \
        f"第二次发出去的不是迁移来的 prompt，实际：{sent[1][:80]}"
    assert r.headers.get("X-Gateway-Jailbreak-Level") == "3"


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


# ═══════════════════════════════════════════════════════════════
# ⑧ 多 key 鉴权 / 不鉴权（runtime 装配带来的语义）
# ═══════════════════════════════════════════════════════════════
def _deps_with(**extra):
    """make_deps 的可变版本 —— 按 runtime 装配的键语义构造 deps。"""
    extra.setdefault("api_key", "test-key")
    pool = Pool()
    cands = extra.pop("candidates", [Candidate(provider="fake", account="k1",
                                               model="gpt-4o")])
    adaptors = extra.pop("adaptors", {("fake", "k1"): OpenAIAdaptor("http://up", "sk-x")})
    return GatewayDeps(
        pool=pool, candidates=cands, adaptors=adaptors,
        call_fn=lambda a, u: {"content": "ok", "finish_reason": "stop",
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                        "total_tokens": 2}},
        meter=TokenMeter(), **extra,
    )


@pytest.mark.asyncio
async def test_多把key_任一把都放行():
    deps = _deps_with(api_key="", api_keys=["k1", "k2"])
    async with client_for(deps) as c:
        for key in ("k1", "k2"):
            r = await c.post("/v1/chat/completions", json=body(),
                             headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 200, f"key={key} 应放行"


@pytest.mark.asyncio
async def test_没配任何key_不鉴权():
    """config.auth_keys 留空的既定语义：不鉴权（仅本机场景）。"""
    deps = _deps_with(api_key="", api_keys=[])
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body())   # 无 Authorization 头
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_on_request_done_收到trace与usage():
    got = []
    deps = _deps_with(on_request_done=got.append)
    async with client_for(deps) as c:
        await c.post("/v1/chat/completions", json=body(), headers=HDR)
    assert len(got) == 1
    d = got[0]
    assert d["requested"] == "gpt-4o"
    assert d["selected"] == "fake:k1/gpt-4o"
    assert d["usage"]["total_tokens"] == 2
    assert d["candidates"], "候选链不能为空"


@pytest.mark.asyncio
async def test_适配器查找_账号键优先于型号键():
    """同 provider 双账号：选中的候选必须拿到自己账号的适配器（各自的 key）。

    做法：k1 打成配额耗尽退避 → 路由只剩 k2 → call_fn 捕获到的适配器
    必须是 ("fake","k2") 那个实例（api_key=sk-2）。
    """
    seen = {}

    def call_fn(adaptor, unified):
        seen["key"] = adaptor.api_key
        return {"content": "ok", "finish_reason": "stop",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}}

    pool = Pool()
    k1 = Candidate(provider="fake", account="k1", model="gpt-4o")
    k2 = Candidate(provider="fake", account="k2", model="gpt-4o")
    # k1 配额耗尽（402）→ 账号级退避，只剩 k2。
    # 消息必须命中 _QUOTA_MARKERS（insufficient quota 等）——
    # 写 "Insufficient Balance" 会被分类成 client 错误、不触发退避
    pool.record_failure(k1, ProviderError("You have insufficient quota", status=402),
                        lease=pool.acquire(k1))
    deps = GatewayDeps(
        pool=pool, candidates=[k1, k2],
        adaptors={("fake", "k1"): OpenAIAdaptor("http://up", "sk-1"),
                  ("fake", "k2"): OpenAIAdaptor("http://up", "sk-2")},
        call_fn=call_fn, meter=TokenMeter(), api_key="test-key",
    )
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(), headers=HDR)
    assert r.status_code == 200
    assert seen["key"] == "sk-2", "选了 k2 却用了别的账号的 key —— 轮巡就废了"




# ═══════════════════════════════════════════════════════════════
# ⑨ 流式重写：破甲阶梯 + OpenAI 流式标准
# ═══════════════════════════════════════════════════════════════
REFUSAL_EVENTS = [{"delta": "I'm sorry, but I can't help with that.",
                   "finish_reason": None},
                  {"delta": "", "finish_reason": "stop"}]
ANSWER_EVENTS = [{"delta": "答"}, {"delta": "案"},
                 {"delta": "", "finish_reason": "stop"},
                 {"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                            "total_tokens": 15}}]


def _stream_deps(resp, *, jailbreak=True, **extra):
    """流式打桩：resp(call_index, unified) -> 事件列表。calls 记录每次 unified。"""
    calls = []

    def _unified_to_result(events):
        deltas, fr, usage = [], None, None
        for ev in events:
            if ev.get("delta"):
                deltas.append(ev["delta"])
            fr = ev.get("finish_reason") or fr
            usage = ev.get("usage") or usage
        return {"content": "".join(deltas), "finish_reason": fr,
                "usage": usage or {}}

    def stream_fn(adaptor, unified):
        calls.append(json.loads(json.dumps(unified)))   # 深拷贝防后续改动
        yield from resp(len(calls), unified)

    def call_fn(adaptor, unified):
        calls.append(json.loads(json.dumps(unified)))
        return _unified_to_result(resp(len(calls), unified))

    pool = Pool()
    cands = [Candidate(provider="fake", account="k1", model="gpt-4o")]
    deps = GatewayDeps(
        pool=pool, candidates=cands,
        adaptors={("fake", "gpt-4o"): OpenAIAdaptor("http://up", "sk")},
        call_fn=call_fn, stream_fn=stream_fn, meter=TokenMeter(),
        api_key="test-key", jailbreak_enabled=jailbreak, **extra)
    return deps, calls


def _parse_sse(text: str) -> list:
    """data: 行 → JSON 列表（[DONE] 转成字符串）。"""
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            out.append(raw.strip() if raw.strip() == "[DONE]"
                       else json.loads(raw))
    return out


async def _stream_post(c, payload=None):
    async with c.stream("POST", "/v1/chat/completions",
                        json=payload or body(stream=True), headers=HDR) as r:
        assert r.status_code == 200
        headers = dict(r.headers)
        text = "".join([chunk async for chunk in r.aiter_text()])
    return headers, text


@pytest.mark.asyncio
async def test_流式_破甲升级后回放():
    """L0 被拒 → L1 变形重试 → 成功内容按 SSE 回放，含完整标准帧。"""
    deps, calls = _stream_deps(lambda i, u: REFUSAL_EVENTS if i == 1 else ANSWER_EVENTS)
    async with client_for(deps) as c:
        headers, text = await _stream_post(c)
    frames = _parse_sse(text)

    # 上游确实先收到原文、再收到变形后的 prompt —— 破甲在流式下真实生效
    assert calls[0]["messages"][-1]["content"] == "你好"
    assert calls[1]["messages"][-1]["content"] != "你好"
    assert headers["x-gateway-jailbreak-level"] == "1"

    # OpenAI 流式标准帧：role 首块 → 内容块 → finish_reason 块 → usage 块 → [DONE]
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    content = "".join(f["choices"][0]["delta"].get("content", "")
                      for f in frames[1:-2] if f != "[DONE]" and f["choices"])
    assert content == "答案"
    fin = [f for f in frames if f != "[DONE]" and f["choices"]
           and f["choices"][0]["finish_reason"]]
    assert fin and fin[0]["choices"][0]["finish_reason"] == "stop"
    usage_frames = [f for f in frames if f != "[DONE]" and f.get("usage")]
    assert usage_frames and usage_frames[0]["usage"]["total_tokens"] == 15
    assert frames[-1] == "[DONE]"
    assert text.rstrip().endswith("[DONE]")


@pytest.mark.asyncio
async def test_流式_全部拒绝_回放最后一次内容():
    deps, _calls = _stream_deps(lambda i, u: REFUSAL_EVENTS)
    async with client_for(deps) as c:
        headers, text = await _stream_post(c)
    frames = _parse_sse(text)
    content = "".join(f["choices"][0]["delta"].get("content", "")
                      for f in frames if f != "[DONE]" and f["choices"])
    assert "I'm sorry" in content          # 用户有权看到模型说了什么（与非流式一致）
    assert frames[-1] == "[DONE]"


@pytest.mark.asyncio
async def test_流式_破甲关闭_真直通不变形():
    """jb 关 = 真·逐 delta 直通（无缓冲无重试），上游收到原始 messages。"""
    deps, calls = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    async with client_for(deps) as c:
        headers, text = await _stream_post(c)
    assert len(calls) == 1
    assert calls[0]["messages"][-1]["content"] == "你好"   # 原样
    assert headers["x-gateway-jailbreak-level"] == "0"
    frames = _parse_sse(text)
    content = "".join(f["choices"][0]["delta"].get("content", "")
                      for f in frames if f != "[DONE]" and f["choices"])
    assert content == "答案"
    assert [f for f in frames if f != "[DONE]" and f.get("usage")]


@pytest.mark.asyncio
async def test_流式_没给max_tokens_不注入():
    seen = {}

    def resp(i, u):
        seen["has_max_tokens"] = "max_tokens" in u
        return ANSWER_EVENTS

    deps, _ = _stream_deps(resp, jailbreak=False)
    async with client_for(deps) as c:
        await _stream_post(c)
    assert seen["has_max_tokens"] is False, "流式同样不注入 max_tokens"


@pytest.mark.asyncio
async def test_流式_截断不重试直接回放():
    """finish_reason=length 且 content 空 = 没想完，不是拒绝 —— 不烧重试。"""
    def resp(i, u):
        return [{"delta": "", "finish_reason": "length"}]

    deps, calls = _stream_deps(resp)
    async with client_for(deps) as c:
        headers, text = await _stream_post(c)
    assert len(calls) == 1, "截断不该重试"
    frames = _parse_sse(text)
    fin = [f for f in frames if f != "[DONE]" and f["choices"]
           and f["choices"][0]["finish_reason"]]
    assert fin[0]["choices"][0]["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_流式_on_request_done_带level与usage():
    got = []
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS if i > 1 else REFUSAL_EVENTS,
                           on_request_done=got.append)
    async with client_for(deps) as c:
        await _stream_post(c)
    assert len(got) == 1
    assert got[0]["jailbreak_level"] == 1
    assert got[0]["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_流式_temperature透传():
    seen = {}

    def resp(i, u):
        seen["temperature"] = u.get("temperature")
        return ANSWER_EVENTS

    deps, _ = _stream_deps(resp, jailbreak=False)
    async with client_for(deps) as c:
        await _stream_post(c, {"model": "gpt-4o", "stream": True,
                               "temperature": 0.3,
                               "messages": [{"role": "user", "content": "你好"}]})
    assert seen["temperature"] == 0.3


@pytest.mark.asyncio
async def test_流式_上游故障_换账号级语义与非流式一致():
    """quota 错误 → 换候选（非流式的既定语义），而不是耗在破甲上。"""
    from gateway.providers.base import ProviderError

    calls = []

    def stream_fn(adaptor, unified):
        calls.append(1)
        if adaptor.api_key == "sk-1":            # k1 配额耗尽
            raise ProviderError("insufficient quota", status=402)
        yield from ANSWER_EVENTS                  # k2 正常

    k1 = Candidate(provider="fake", account="k1", model="gpt-4o")
    k2 = Candidate(provider="fake", account="k2", model="gpt-4o")
    deps = GatewayDeps(
        pool=Pool(), candidates=[k1, k2],
        adaptors={("fake", "k1"): OpenAIAdaptor("http://up", "sk-1"),
                  ("fake", "k2"): OpenAIAdaptor("http://up", "sk-2")},
        stream_fn=stream_fn, meter=TokenMeter(), api_key="test-key",
        jailbreak_enabled=False,
    )
    async with client_for(deps) as c:
        r = await c.post("/v1/chat/completions", json=body(stream=True), headers=HDR)
    assert r.status_code == 200
    assert len(calls) == 2, "quota 应换下一个候选"
    frames = _parse_sse(r.text)
    content = "".join(f["choices"][0]["delta"].get("content", "")
                      for f in frames if f != "[DONE]" and f["choices"])
    assert content == "答案"


# ═══════════════════════════════════════════════════════════════
# ⑩ /v1/models
# ═══════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_v1_models_openai形状():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    async with client_for(deps) as c:
        r = await c.get("/v1/models", headers=HDR)
    assert r.status_code == 200
    j = r.json()
    assert j["object"] == "list"
    ids = [m["id"] for m in j["data"]]
    assert "gpt-4o" in ids and all(m["object"] == "model" for m in j["data"])


@pytest.mark.asyncio
async def test_v1_models_anthropic形状_带x_api_key时():
    deps, _ = _stream_deps(lambda i, u: ANSWER_EVENTS, jailbreak=False)
    async with client_for(deps) as c:
        r = await c.get("/v1/models", headers={**HDR, "x-api-key": "test-key",
                                               "anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    j = r.json()
    assert j["data"][0]["type"] == "model"
    assert "gpt-4o" in [m["id"] for m in j["data"]]
