"""G3 路由层测试。

核心要验证的一件事：**配额耗尽 vs 服务故障是两种不同的失败**，
处理方式完全不同 ——
  配额耗尽 → 只跳过那个账号，同 provider 其他 key 继续用
  服务故障 → 跳过整个 provider
这是三层降级能成立的前提。分不清，降级链就是错的。
"""

from __future__ import annotations

import pytest

from gateway.providers.base import ProviderError
from gateway.routing.pool import (
    Candidate,
    Pool,
    RouteState,
    RoutingPolicy,
    DEFAULT_WEIGHTS,
)


# ═══════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════
def cand(provider: str, account: str, model: str, latency: float = 100.0) -> Candidate:
    return Candidate(provider=provider, account=account, model=model, latency_ms=latency)


def three_accounts():
    """一个 provider 三个账号，同型号 —— 最典型的多 key 场景。"""
    return [
        cand("deepseek", "key1", "deepseek-chat"),
        cand("deepseek", "key2", "deepseek-chat"),
        cand("deepseek", "key3", "deepseek-chat"),
    ]


# ═══════════════════════════════════════════════════════════════
# ① 健康路由排在故障路由前
# ═══════════════════════════════════════════════════════════════
def test_健康路由排在故障路由前():
    pool = Pool()
    good = cand("openai", "k1", "gpt-4o")
    bad = cand("anthropic", "k1", "claude-3")

    pool.record_success(good, latency_ms=80, lease=pool.acquire(good))
    # 让 bad 失败两次 —— 不触发冷却（阈值是 3），但健康分已经掉下来。
    # 这样两条路由都还在候选里，才能验证「排序」而不是「排除」。
    for _ in range(2):
        pool.record_failure(bad, ProviderError("boom", status=500), lease=pool.acquire(bad))

    ranked = pool.rank_targets([bad, good], RoutingPolicy())
    assert len(ranked) == 2, "两条都该在候选里（没到冷却阈值）"
    assert ranked[0].provider == "openai", "健康的路由必须排前面"
    assert ranked[1].provider == "anthropic"


def test_从没打过的路由按中性分排在健康和非健康之间():
    """没历史 ≠ 坏。冷启动要给中性分，不能直接当故障排到末尾 ——
    否则新加的 key 永远轮不上（这正是 KXNS 那套「冷启动悖论」）。"""
    pool = Pool()
    known_good = cand("openai", "k1", "gpt-4o")
    fresh = cand("moonshot", "k1", "kimi")

    for _ in range(5):
        pool.record_success(known_good, latency_ms=80, lease=pool.acquire(known_good))

    ranked = pool.rank_targets([known_good, fresh], RoutingPolicy())
    # 两者都能进候选，公平分不会太差
    scores = {c.provider: pool.score(c, RoutingPolicy()) for c in ranked}
    assert scores["moonshot"] > 0.3, "冷启动路由不该被判死刑"
    assert scores["openai"] > scores["moonshot"]


# ═══════════════════════════════════════════════════════════════
# ② ★ 配额耗尽只跳账号，不跳 provider
# ═══════════════════════════════════════════════════════════════
def test_配额耗尽只跳过该账号():
    pool = Pool()
    c = three_accounts()
    k1, k2, k3 = c

    # key1 配额耗尽（402 + 文案）
    pool.record_failure(k1, ProviderError("insufficient credits", status=402),
                        lease=pool.acquire(k1))

    ranked = pool.rank_targets(c, RoutingPolicy())
    names = [f"{x.provider}/{x.account}" for x in ranked]

    assert "deepseek/key1" not in names, "配额耗尽的账号必须被跳过"
    assert "deepseek/key2" in names, "同 provider 其他账号必须仍然可用"
    assert "deepseek/key3" in names


def test_配额耗尽不触发_provider_级冷却():
    """这是最容易写错的一条：如果配额耗尽也标记 provider 冷却，
    那整个 deepseek 都被封掉，key2/key3 明明还能用却调不动。"""
    pool = Pool()
    k1 = cand("deepseek", "key1", "deepseek-chat")
    pool.record_failure(k1, ProviderError("quota exhausted", status=429),
                        lease=pool.acquire(k1))

    assert pool.is_account_backoff("deepseek", "key1") is True
    assert pool.is_cooled("deepseek") is False, "配额耗尽不该让整个 provider 进冷却"


def test_服务故障让整个_provider_进冷却():
    """反过来：5xx 是 provider 病了，整个 provider 该被跳过。"""
    pool = Pool()
    k1 = cand("deepseek", "key1", "deepseek-chat")
    for _ in range(3):
        pool.record_failure(k1, ProviderError("server error", status=503),
                            lease=pool.acquire(k1))

    assert pool.is_cooled("deepseek") is True, "连续 5xx 必须让 provider 进冷却"
    assert pool.is_account_backoff("deepseek", "key1") is False, "服务故障不是账号问题"


def test_客户端错误不该污染任何状态():
    """400 请求体坏 / 404 模型不存在 —— 这是调用方的错，不是 provider 也不是账号。"""
    pool = Pool()
    k1 = cand("openai", "key1", "gpt-4o")
    for _ in range(10):
        pool.record_failure(k1, ProviderError("bad request", status=400),
                            lease=pool.acquire(k1))

    assert pool.is_cooled("openai") is False
    assert pool.is_account_backoff("openai", "key1") is False
    st = pool.state(k1)
    assert st.consecutive_failures == 0, "客户端错误不该累加失败计数"


# ═══════════════════════════════════════════════════════════════
# ③ 连续失败 N 次后进冷却
# ═══════════════════════════════════════════════════════════════
def test_连续失败三次进冷却():
    pool = Pool()
    k = cand("openai", "k1", "gpt-4o")

    def fail():
        pool.record_failure(k, ProviderError("boom", status=500), lease=pool.acquire(k))

    fail(); fail()
    assert pool.is_cooled("openai") is False, "两次还不够"
    fail()
    assert pool.is_cooled("openai") is True, "第三次触发冷却"


def test_成功一次清零失败计数():
    pool = Pool()
    k = cand("openai", "k1", "gpt-4o")

    pool.record_failure(k, ProviderError("boom", status=500), lease=pool.acquire(k))
    pool.record_failure(k, ProviderError("boom", status=500), lease=pool.acquire(k))
    assert pool.state(k).consecutive_failures == 2

    pool.record_success(k, latency_ms=90, lease=pool.acquire(k))
    assert pool.state(k).consecutive_failures == 0


def test_冷却阈值可配():
    pool = Pool(cooldown_threshold=1)
    k = cand("openai", "k1", "gpt-4o")
    pool.record_failure(k, ProviderError("boom", status=500), lease=pool.acquire(k))
    assert pool.is_cooled("openai") is True


# ═══════════════════════════════════════════════════════════════
# ④ 租约：同一路由不被并发二次分配
# ═══════════════════════════════════════════════════════════════
def test_租约期间同一路由不被二次分配():
    """防「同一个死路由被并发打」—— 第一个请求还没回来，
    第二个请求不该又挑中它。"""
    pool = Pool()
    k = cand("openai", "k1", "gpt-4o")

    lease1 = pool.acquire(k)
    assert lease1 is not None, "第一次能拿到"

    lease2 = pool.acquire(k)
    assert lease2 is None, "租约没释放前不能再拿"


def test_释放后可以重新获取():
    pool = Pool()
    k = cand("openai", "k1", "gpt-4o")
    lease = pool.acquire(k)
    pool.release(lease)
    assert pool.acquire(k) is not None


def test_过期租约自动失效():
    """请求挂死时租约不能被永久占着 —— 过期要能自动回收。"""
    pool = Pool(lease_ttl=0.0)          # 立即过期
    k = cand("openai", "k1", "gpt-4o")
    pool.acquire(k)
    assert pool.acquire(k) is not None, "过期租约不该阻塞"


def test_乱序回来的失败不覆盖新状态():
    """老请求晚回来，不能拿它去覆盖新租约的状态 —— 这是 circuit breaker 的经典 bug。"""
    pool = Pool()
    k = cand("openai", "k1", "gpt-4o")
    old_lease = pool.acquire(k)
    pool.release(old_lease)
    new_lease = pool.acquire(k)          # 新一代租约
    assert new_lease is not None and new_lease.generation > old_lease.generation

    # 老租约的失败上报应该被拒
    applied = pool.record_failure(k, ProviderError("boom", status=500), lease=old_lease)
    assert applied is False, "过期租约的上报必须被丢弃"


# ═══════════════════════════════════════════════════════════════
# ⑤ 权重改变排序
# ═══════════════════════════════════════════════════════════════
def test_权重改动能改变排序():
    """A 健康但慢，B 一般但快。把延迟权重拉满，B 应该跑到前面。"""
    pool = Pool()
    fast = cand("fastprov", "k1", "m", latency=50.0)
    healthy = cand("healthprov", "k1", "m", latency=900.0)

    for _ in range(5):
        pool.record_success(healthy, latency_ms=900, lease=pool.acquire(healthy))
    pool.record_success(fast, latency_ms=50, lease=pool.acquire(fast))
    pool.record_failure(fast, ProviderError("boom", status=500), lease=pool.acquire(fast))

    # 默认权重：健康优先 → healthy 在前
    ranked_default = pool.rank_targets([fast, healthy], RoutingPolicy())
    assert ranked_default[0].provider == "healthprov"

    # 延迟权重拉满 → fast 在前
    latency_heavy = RoutingPolicy(weights={"health": 0.0, "quota": 0.0,
                                           "latency": 1.0, "fairness": 0.0})
    ranked_latency = pool.rank_targets([fast, healthy], latency_heavy)
    assert ranked_latency[0].provider == "fastprov"


def test_默认权重和为_1():
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9


def test_负权重被拒绝():
    with pytest.raises(ValueError):
        RoutingPolicy(weights={"health": -1.0})


# ═══════════════════════════════════════════════════════════════
# ⑥ 排除失效候选
# ═══════════════════════════════════════════════════════════════
def test_冷却中的_provider_不出现在候选里():
    pool = Pool()
    dead = cand("deadprov", "k1", "m")
    alive = cand("aliveprov", "k1", "m")
    for _ in range(3):
        pool.record_failure(dead, ProviderError("boom", status=503), lease=pool.acquire(dead))

    ranked = pool.rank_targets([dead, alive], RoutingPolicy())
    assert [c.provider for c in ranked] == ["aliveprov"]


def test_全部失效时返回空列表():
    """全挂时返回空 —— 让上层决定报错还是等，而不是瞎选一个。"""
    pool = Pool()
    a = cand("p1", "k1", "m")
    b = cand("p2", "k1", "m")
    for c in (a, b):
        for _ in range(3):
            pool.record_failure(c, ProviderError("boom", status=503), lease=pool.acquire(c))
    assert pool.rank_targets([a, b], RoutingPolicy()) == []


def test_公平权重让少用的路由优先():
    """两个等同的健康路由，用得少的应该排前面 —— 否则流量全压一个。"""
    pool = Pool()
    a = cand("provA", "k1", "m")
    b = cand("provB", "k1", "m")
    for _ in range(5):
        pool.record_success(a, latency_ms=100, lease=pool.acquire(a))
    for _ in range(1):
        pool.record_success(b, latency_ms=100, lease=pool.acquire(b))

    fair_policy = RoutingPolicy(weights={"health": 0.2, "quota": 0.1,
                                         "latency": 0.1, "fairness": 0.6})
    ranked = pool.rank_targets([a, b], fair_policy)
    assert ranked[0].provider == "provB", "用得少的该排前面"
