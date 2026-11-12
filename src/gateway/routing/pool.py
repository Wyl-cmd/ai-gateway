"""G3 路由层 —— 选谁去发这个请求。

**这一层只「选」，不「调」**。调是 G2 转发层的事。分开的理由：
路由的核心是「决策可解释」（哪个候选被排除、为什么），一旦和网络请求混在
一起，就再也说不清「为什么这次走了 anthropic」。

## 三种失败状态必须分开存

抄 freellmpool 的机制，但把它的两套状态讲清楚：

    _provider_cooldown  provider 级冷却   —— 5xx 连续 N 次 → 整家跳过
    _account_backoff    账号级退避        —— 配额耗尽 → 只跳这个 key

    客户端错误（400/401/403/404）        —— 什么状态都不写

**为什么不能合并成一个状态**：
  配额耗尽的状态码是 402/429，按状态码它「像」服务故障。如果合并处理，
  一个 key 用完额度就会把整家 provider 全封掉 —— 明明还有 key2/key3 能用。
  这是三层降级里「账号轮巡」这一层存在的意义。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from ..providers.base import (
    ProviderError,
    classify_failure,
    is_health_failure,
    is_quota_exhausted,
)


# ═══════════════════════════════════════════════════════════════
# 默认参数
# ═══════════════════════════════════════════════════════════════
#: 评分权重，四项和为 1。改权重能直接改变排序（G7 模式层会用到）
DEFAULT_WEIGHTS: dict[str, float] = {
    "health": 0.45,      # 历史成功率
    "quota": 0.20,       # 额度还够不够
    "latency": 0.20,     # 快不快
    "fairness": 0.15,    # 用得少的优先（防流量全压一个）
}

#: 连续健康故障多少次触发 provider 冷却
DEFAULT_COOLDOWN_THRESHOLD = 3

#: provider 冷却时长（秒）
DEFAULT_COOLDOWN_SECONDS = 60.0

#: 账号退避时长（秒）。抄 freellmpool 的 15 分钟 —— 免费额度按天/按月重置，
#: 15 分钟内重试同一个耗尽的 key 没有意义。
DEFAULT_ACCOUNT_BACKOFF_SECONDS = 15 * 60.0

#: 租约有效期（秒）。请求挂死时不能被永久占着。
DEFAULT_LEASE_TTL = 120.0

#: 冷启动的中性健康分。既不是 1.0（会让新路由抢光流量），
#: 也不是 0.0（会让新路由永远轮不上）。
COLD_START_HEALTH = 0.5


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Candidate:
    """一个可选目标：某家 provider 的某个账号上的某个型号。

    frozen 是有意的 —— 候选对象不该被就地修改，状态统一存在 Pool 里，
    否则两处状态会漂移。
    """

    provider: str
    account: str
    model: str
    latency_ms: float = 100.0
    rpd: int = 0                       # 每日限额，0 = 不限

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.account, self.model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.account}/{self.model}"


@dataclass
class RouteState:
    """单条路由的历史战绩。"""

    successes: int = 0
    failures: int = 0
    total: int = 0
    consecutive_failures: int = 0
    uses: int = 0                      # 被选中次数（公平分用）
    last_latency_ms: float = 0.0

    @property
    def success_rate(self) -> float:
        """从没打过 → 中性分，不是 1.0 也不是 0.0。"""
        return COLD_START_HEALTH if self.total == 0 else self.successes / self.total


@dataclass(frozen=True)
class Lease:
    """一次尝试的「所有权凭证」。

    带 generation 是为了防止**乱序上报**：老请求超时后晚回来，
    不能拿它的失败去覆盖新一代租约的状态 —— 这是 circuit breaker 的经典 bug，
    会让刚恢复的路由立刻又被打回故障。
    """

    key: tuple[str, str, str]
    generation: int
    acquired_at: float


@dataclass(frozen=True)
class RoutingPolicy:
    """评分策略。"""

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def __post_init__(self) -> None:
        for k, v in self.weights.items():
            if v < 0:
                raise ValueError(f"权重不能为负: {k}={v}")
        unknown = set(self.weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"未知的权重项: {unknown}")
        # 允许不写全（缺的按 0 算），但和不能超过 1
        if sum(self.weights.values()) > 1.0 + 1e-9:
            raise ValueError(f"权重和不能超过 1: {sum(self.weights.values())}")

    def w(self, name: str) -> float:
        return self.weights.get(name, 0.0)


# ═══════════════════════════════════════════════════════════════
# Pool
# ═══════════════════════════════════════════════════════════════
class Pool:
    """候选池 + 状态机。线程安全（一把可重入锁）。"""

    def __init__(
        self,
        *,
        cooldown_threshold: int = DEFAULT_COOLDOWN_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        account_backoff_seconds: float = DEFAULT_ACCOUNT_BACKOFF_SECONDS,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        clock=time.monotonic,
    ) -> None:
        self.cooldown_threshold = cooldown_threshold
        self.cooldown_seconds = cooldown_seconds
        self.account_backoff_seconds = account_backoff_seconds
        self.lease_ttl = lease_ttl
        self._clock = clock

        self._lock = threading.RLock()
        self._states: dict[tuple[str, str, str], RouteState] = {}
        self._generation: dict[tuple[str, str, str], int] = {}
        self._leases: dict[tuple[str, str, str], Lease] = {}
        self._provider_cooldown: dict[str, float] = {}
        self._account_backoff: dict[tuple[str, str], float] = {}

    # ── 状态读取 ──
    def state(self, c: Candidate) -> RouteState:
        with self._lock:
            return self._states.setdefault(c.key, RouteState())

    def is_cooled(self, provider: str) -> bool:
        with self._lock:
            until = self._provider_cooldown.get(provider, 0.0)
            return until > self._clock()

    def is_account_backoff(self, provider: str, account: str) -> bool:
        with self._lock:
            until = self._account_backoff.get((provider, account), 0.0)
            return until > self._clock()

    # ── 租约 ──
    def acquire(self, c: Candidate) -> Lease | None:
        """拿一条路由的尝试权。已被占用且未过期 → None。"""
        with self._lock:
            now = self._clock()
            held = self._leases.get(c.key)
            if held is not None:
                if now - held.acquired_at < self.lease_ttl:
                    return None                      # 还在租期内
                # 过期租约自动回收（请求挂死的情况）
            gen = self._generation.get(c.key, 0) + 1
            self._generation[c.key] = gen
            lease = Lease(key=c.key, generation=gen, acquired_at=now)
            self._leases[c.key] = lease
            return lease

    def release(self, lease: Lease | None) -> None:
        if lease is None:
            return
        with self._lock:
            held = self._leases.get(lease.key)
            if held is not None and held.generation == lease.generation:
                del self._leases[lease.key]

    def _lease_valid(self, c: Candidate, lease: Lease | None) -> bool:
        """上报是否仍是当前这一代。没有 lease 的一律放行（便于直接调用）。"""
        if lease is None:
            return True
        return self._generation.get(c.key, 0) == lease.generation

    # ── 战绩上报 ──
    def record_success(self, c: Candidate, latency_ms: float,
                       lease: Lease | None = None) -> bool:
        with self._lock:
            if not self._lease_valid(c, lease):
                return False
            st = self._states.setdefault(c.key, RouteState())
            st.successes += 1
            st.total += 1
            st.consecutive_failures = 0
            st.uses += 1
            st.last_latency_ms = latency_ms
            # 成功就解掉这家 provider 的冷却（它好了）
            self._provider_cooldown.pop(c.provider, None)
            return True

    def record_failure(self, c: Candidate, exc: Exception,
                       lease: Lease | None = None) -> bool:
        """上报一次失败。返回 False 表示这条上报因为租约过期被丢弃。

        **三种失败三种处理**（这是整层的核心）：
          quota  → 只标账号退避，不累加 consecutive，不碰 provider 冷却
          health → 累加 consecutive，达阈值标 provider 冷却
          client → 什么都不写（调用方自己的错）
        """
        with self._lock:
            if not self._lease_valid(c, lease):
                return False

            now = self._clock()
            kind = classify_failure(exc)

            if kind == "client":
                # 400 参数错 / 401 鉴权 / 404 模型不存在 —— 都不是 provider 的问题。
                # 写状态会让一个坏请求把整家 provider 打成故障。
                return True

            if kind == "quota":
                # 只影响这个账号；不动 consecutive_failures，不动 provider 冷却。
                self._account_backoff[(c.provider, c.account)] = (
                    now + self.account_backoff_seconds
                )
                st = self._states.setdefault(c.key, RouteState())
                st.uses += 1
                return True

            # kind == "health"
            st = self._states.setdefault(c.key, RouteState())
            st.failures += 1
            st.total += 1
            st.consecutive_failures += 1
            st.uses += 1

            if st.consecutive_failures >= self.cooldown_threshold:
                until = now + self.cooldown_seconds
                self._provider_cooldown[c.provider] = max(
                    self._provider_cooldown.get(c.provider, 0.0), until
                )
            return True

    # ── 评分 ──
    def score(self, c: Candidate, policy: RoutingPolicy) -> float:
        st = self.state(c)

        health = st.success_rate

        quota = 0.0 if self.is_account_backoff(c.provider, c.account) else 1.0

        # 延迟归一化：100ms 为基准，越快越接近 1，越慢越趋近 0
        lat = st.last_latency_ms or c.latency_ms
        latency = 1.0 / (1.0 + lat / 100.0)

        # 用得越少分越高（防流量全压一个）
        fairness = 1.0 / (1.0 + st.uses)

        return (
            policy.w("health") * health
            + policy.w("quota") * quota
            + policy.w("latency") * latency
            + policy.w("fairness") * fairness
        )

    # ── 排序 ──
    def rank_targets(
        self,
        candidates: Iterable[Candidate],
        policy: RoutingPolicy | None = None,
    ) -> list[Candidate]:
        """返回排好序的候选列表。

        被排除的：provider 在冷却、账号在退避。
        全失效时返回空列表 —— 让上层决定报错还是等，而不是瞎选一个。
        """
        policy = policy or RoutingPolicy()
        out: list[Candidate] = []
        with self._lock:
            for c in candidates:
                if self.is_cooled(c.provider):
                    continue
                if self.is_account_backoff(c.provider, c.account):
                    continue
                out.append(c)
        # 分数降序；同分按 key 排（保证结果稳定可复现，测试才不会飘）
        out.sort(key=lambda c: (-self.score(c, policy), c.key))
        return out

    # ── 解释（G6 trace 会用到）──
    def explain(self, candidates: Iterable[Candidate],
                policy: RoutingPolicy | None = None) -> list[dict]:
        """每个候选为什么被留下/被排除 —— 给 route_trace 用。"""
        policy = policy or RoutingPolicy()
        rows = []
        for c in candidates:
            if self.is_cooled(c.provider):
                rows.append({"candidate": str(c), "kept": False,
                             "reason": "provider 冷却中"})
            elif self.is_account_backoff(c.provider, c.account):
                rows.append({"candidate": str(c), "kept": False,
                             "reason": "账号配额耗尽，退避中"})
            else:
                rows.append({"candidate": str(c), "kept": True,
                             "reason": "入选", "score": round(self.score(c, policy), 4)})
        return rows
