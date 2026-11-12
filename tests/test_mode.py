"""G7 模式层测试（auto / manual + 三档寻址 + 三种降级策略）。

这一层回答一个问题：**这次请求允许走多远**。

寻址三档（越写越具体）：
    gpt-4o               只锁型号，随便哪家哪号
    openai/gpt-4o        锁型号 + 锁 provider
    openai:key2/gpt-4o   锁型号 + 锁 provider + 锁账号

降级三策略（Master 2026-11-11 定的，默认 strict）：
    strict              一步不许降，指定的走不了就报错
    fallback_same_model 允许换家（同型号，参数语义一致）
    fallback_any_model  允许换型号（能力变了，必须告警）

**「换供应商」和「换型号」必须分开**，这是整套设计的核心：
前者你要的东西没变，后者变了。混成一个概念，降级就变成了静默替换。
"""

from __future__ import annotations

import pytest

from gateway.mode.manual import FallbackPolicy, ManualTarget, TargetLock, parse_target
from gateway.mode.router import ModeRouter, RouteFailed


# ═══════════════════════════════════════════════════════════════
# ① 三档寻址解析
# ═══════════════════════════════════════════════════════════════
def test_只写型号只锁型号():
    t = parse_target("gpt-4o")
    assert t.model == "gpt-4o"
    assert t.provider is None
    assert t.account is None
    assert t.lock == TargetLock.MODEL_ONLY


def test_provider_斜杠型号锁_provider():
    t = parse_target("openai/gpt-4o")
    assert t.provider == "openai"
    assert t.model == "gpt-4o"
    assert t.account is None
    assert t.lock == TargetLock.PROVIDER


def test_provider_冒号账号斜杠型号锁账号():
    t = parse_target("openai:key2/gpt-4o")
    assert t.provider == "openai"
    assert t.account == "key2"
    assert t.model == "gpt-4o"
    assert t.lock == TargetLock.ACCOUNT


def test_型号名里带斜杠也能解析():
    """有些上游型号本身带斜杠（meta-llama/Llama-3-70B）——
    只有明确是「已知 provider 前缀」时才按 provider 切。"""
    t = parse_target("meta-llama/Llama-3-70B")
    # 没有 provider 前缀声明，整串当型号
    assert t.model == "meta-llama/Llama-3-70B"
    assert t.provider is None


def test_已知_provider_前缀才按_provider_切():
    t = parse_target("openai/meta-llama/Llama-3", known_providers={"openai"})
    assert t.provider == "openai"
    assert t.model == "meta-llama/Llama-3"


def test_空字符串抛错():
    with pytest.raises(ValueError):
        parse_target("")


def test_只有_provider_没型号抛错():
    with pytest.raises(ValueError):
        parse_target("openai/")


# ═══════════════════════════════════════════════════════════════
# ② 降级策略语义
# ═══════════════════════════════════════════════════════════════
def test_三种策略的值固定():
    assert FallbackPolicy.STRICT == "strict"
    assert FallbackPolicy.SAME_MODEL == "fallback_same_model"
    assert FallbackPolicy.ANY_MODEL == "fallback_any_model"


def test_默认策略是_strict():
    """默认必须是最保守的 —— 用户没说要降级就不该降。"""
    from gateway.config import ModeConfig
    assert ModeConfig.model_fields["fallback"].default == "strict"


# ═══════════════════════════════════════════════════════════════
# ③ ModeRouter：auto 与 manual
# ═══════════════════════════════════════════════════════════════
def _pool_with(*specs):
    from gateway.routing.pool import Candidate, Pool
    p = Pool()
    cands = [Candidate(provider=x[0], account=x[1], model=x[2]) for x in specs]
    return p, cands


def test_auto_模式不锁任何东西():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("deepseek", "k1", "deepseek-chat"),
    )
    r = ModeRouter(pool=pool)
    picked = r.plan(mode="auto", candidates=cands)
    assert len(picked) == 2, "auto 模式下全部候选都该进备选队列"


def test_manual_只写型号_同型号的多家都进队列():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("azure", "k1", "gpt-4o"),
        ("deepseek", "k1", "deepseek-chat"),
    )
    r = ModeRouter(pool=pool)
    picked = r.plan(mode="manual", target="gpt-4o", candidates=cands,
                    policy=FallbackPolicy.SAME_MODEL)
    models = {c.model for c in picked}
    assert models == {"gpt-4o"}, "只该留 gpt-4o，不该混进 deepseek-chat"
    assert len(picked) == 2, "两家都有 gpt-4o，都该在"


def test_manual_锁_provider_后其他家不进队列():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("azure", "k1", "gpt-4o"),
    )
    r = ModeRouter(pool=pool)
    picked = r.plan(mode="manual", target="openai/gpt-4o", candidates=cands,
                    policy=FallbackPolicy.SAME_MODEL)
    assert all(c.provider == "openai" for c in picked)


def test_manual_锁账号后只有一个():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("openai", "k2", "gpt-4o"),
    )
    r = ModeRouter(pool=pool)
    picked = r.plan(mode="manual", target="openai:k2/gpt-4o", candidates=cands,
                    policy=FallbackPolicy.SAME_MODEL)
    assert len(picked) == 1
    assert picked[0].account == "k2"


def test_strict_下无候选就抛错不降级():
    """strict 的全部意义：指定的走不了就明确报错，绝不偷偷换。"""
    pool, cands = _pool_with(("deepseek", "k1", "deepseek-chat"))
    r = ModeRouter(pool=pool)
    with pytest.raises(RouteFailed):
        r.plan(mode="manual", target="openai/gpt-4o", candidates=cands,
               policy=FallbackPolicy.STRICT)


def test_same_model_下会换家但不换型号():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("azure", "k1", "gpt-4o"),
        ("deepseek", "k1", "deepseek-chat"),
    )
    r = ModeRouter(pool=pool)
    # 锁 openai 但 openai 挂了（这里用「候选里没有」模拟）
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))

    picked = r.plan(mode="manual", target="openai/gpt-4o", candidates=cands,
                    policy=FallbackPolicy.SAME_MODEL)
    assert all(c.model == "gpt-4o" for c in picked), "型号不能变"
    assert picked, "openai 挂了应该换到 azure"


def test_any_model_下允许换型号但会标记降级():
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),
        ("deepseek", "k1", "deepseek-chat"),
    )
    r = ModeRouter(pool=pool)
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))

    picked = r.plan(mode="manual", target="openai/gpt-4o", candidates=cands,
                    policy=FallbackPolicy.ANY_MODEL)
    assert picked, "any_model 允许换型号"
    assert picked[0].model != "gpt-4o"


def test_优先同型号再跨型号():
    """any_model 不是「随便挑一个」—— 先把同型号的试完，再跨型号。"""
    pool, cands = _pool_with(
        ("openai", "k1", "gpt-4o"),          # 会被打成故障
        ("azure", "k1", "gpt-4o"),           # 同型号，应该排前面
        ("deepseek", "k1", "deepseek-chat"), # 跨型号，排后面
    )
    r = ModeRouter(pool=pool)
    for _ in range(3):
        pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))

    picked = r.plan(mode="manual", target="openai/gpt-4o", candidates=cands,
                    policy=FallbackPolicy.ANY_MODEL)
    assert picked[0].model == "gpt-4o", "同型号的 azure 必须排在跨型号前面"
    assert picked[0].provider == "azure"


def test_全部候选都没了抛_RouteFailed():
    pool, cands = _pool_with(("openai", "k1", "gpt-4o"))
    r = ModeRouter(pool=pool)
    for _ in range(3):
        pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    with pytest.raises(RouteFailed):
        r.plan(mode="auto", candidates=cands)


def test_RouteFailed_带诊断信息():
    """报错不能只说「失败了」—— 要说清试了几个、为什么都不行。"""
    pool, cands = _pool_with(("openai", "k1", "gpt-4o"))
    r = ModeRouter(pool=pool)
    for _ in range(3):
        pool.record_failure(cands[0], Exception("boom"), lease=pool.acquire(cands[0]))
    try:
        r.plan(mode="auto", candidates=cands)
    except RouteFailed as e:
        assert e.tried, "要记录试过哪些"
        assert str(e), "要有可读消息"


def test_非法_mode_抛错():
    pool, cands = _pool_with(("openai", "k1", "gpt-4o"))
    r = ModeRouter(pool=pool)
    with pytest.raises(ValueError):
        r.plan(mode="乱写", candidates=cands)


def test_manual_缺_target_抛错():
    pool, cands = _pool_with(("openai", "k1", "gpt-4o"))
    r = ModeRouter(pool=pool)
    with pytest.raises(ValueError):
        r.plan(mode="manual", candidates=cands)
