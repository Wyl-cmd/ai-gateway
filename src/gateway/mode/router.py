"""G7 模式路由 —— 决定「这次请求允许走多远」。

auto 与 manual 的区别只有一件事：**候选集怎么收窄**。
收窄之后的排序、排除、失败处理全部复用 G3 的 Pool —— 模式层只决定「叫不叫它」，
不重新实现一遍路由（重复实现必然漂移）。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..routing.pool import Candidate, Pool, RoutingPolicy
from .manual import FallbackPolicy, ManualTarget, TargetLock, parse_target


class RouteFailed(Exception):
    """一个候选都走不了。

    必须带诊断信息 —— 只说「失败了」，运维和用户都无从下手。
    """

    def __init__(self, message: str, tried: Sequence[str] | None = None) -> None:
        super().__init__(message)
        self.tried = list(tried or [])


class ModeRouter:
    def __init__(self, pool: Pool, policy: RoutingPolicy | None = None) -> None:
        self.pool = pool
        self.policy = policy or RoutingPolicy()

    # ── 主入口 ──
    def plan(
        self,
        *,
        mode: str,
        candidates: Iterable[Candidate],
        target: str | None = None,
        policy: FallbackPolicy | str = FallbackPolicy.STRICT,
        known_providers: set[str] | None = None,
        prefer_model: str | None = None,
    ) -> list[Candidate]:
        """返回排好序的可用候选队列（空列表不存在 —— 走不了就抛 RouteFailed）。

        `prefer_model` 是 AUTO 模式的型号亲和约束。**没有它 auto 模式是错的**：
        实测请求 `gpt-4o` 结果返回了 `deepseek-chat` —— 三个候选同分，
        按 key 的字典序排到 deepseek 第一。这等于 auto 模式直接跳到三层降级的
        第三层（跨型号），而那一层本该默认关闭。

        AUTO 的正确语义是**型号亲和**（Master 2026-11-11 定的）：
          ① 先在同型号里按权重排序（账号轮巡 → 换供应商，都算同型号）
          ② 同型号全都不可用，才看 policy 决定要不要跨型号
        """
        cands = list(candidates)
        policy = FallbackPolicy(policy)

        if mode == "auto":
            if prefer_model:
                same = [c for c in cands if c.model == prefer_model]
                picked = self.pool.rank_targets(same, self.policy)
                if picked:
                    return picked
                # 同型号全挂 → 只有显式允许才跨型号
                if policy == FallbackPolicy.STRICT:
                    raise RouteFailed(
                        f"auto 模式下 {prefer_model} 无可用候选（strict 不跨型号）",
                        tried=[str(c) for c in same],
                    )
                if policy == FallbackPolicy.SAME_MODEL:
                    raise RouteFailed(
                        f"auto 模式下找不到其他 {prefer_model} 的可用实例",
                        tried=[str(c) for c in same],
                    )
                # ANY_MODEL：同型号排完接跨型号
                cross = self.pool.rank_targets(
                    [c for c in cands if c.model != prefer_model], self.policy)
                if not cross:
                    raise RouteFailed("auto 模式下没有任何可用候选",
                                      tried=[str(c) for c in cands])
                return cross

            picked = self.pool.rank_targets(cands, self.policy)
            if not picked:
                raise RouteFailed("auto 模式下没有可用候选（全部冷却或退避）",
                                  tried=[str(c) for c in cands])
            return picked

        if mode == "manual":
            if not target:
                raise ValueError("manual 模式必须给 target")
            return self._plan_manual(cands, target, policy, known_providers)

        raise ValueError(f"未知 mode: {mode!r}（只支持 auto / manual）")

    # ── manual ──
    def _plan_manual(
        self,
        cands: list[Candidate],
        target: str,
        policy: FallbackPolicy,
        known_providers: set[str] | None,
    ) -> list[Candidate]:
        t = parse_target(target, known_providers=known_providers)

        # 第一层：同型号同 provider 同账号（最严格）
        exact = self._filter(cands, t)

        if policy == FallbackPolicy.STRICT:
            picked = self.pool.rank_targets(exact, self.policy)
            if not picked:
                raise RouteFailed(
                    f"strict 策略下 {t} 无可用候选（不降级）",
                    tried=[str(c) for c in exact],
                )
            return picked

        # 目标本身能用 → 就用它，不降级。
        #
        # **这一条是「降级」的定义**：目标不行才降，目标行就不降。
        # 第一版写成了「目标能用也把备选都追加进去」，结果
        # `openai/gpt-4o` 明明能走，返回的队列里还塞着 azure 和 deepseek-chat ——
        # 队列一长，上层重试时第一次失败就可能滑到跨型号上去，
        # 用户指定的 provider 锁形同虚设。
        primary = self.pool.rank_targets(exact, self.policy)
        if primary:
            return primary

        # 目标一个都不在 → 按策略往下找
        if policy == FallbackPolicy.SAME_MODEL:
            alt = self.pool.rank_targets(self._same_model_others(cands, t), self.policy)
            if not alt:
                raise RouteFailed(
                    f"fallback_same_model 下找不到其他 {t.model} 的可用实例",
                    tried=[str(c) for c in cands],
                )
            return alt

        # ANY_MODEL：同型号优先，再跨型号
        same = self.pool.rank_targets(self._same_model_others(cands, t), self.policy)
        cross = self.pool.rank_targets(
            [c for c in cands if c.model != t.model], self.policy)
        out = same + cross
        if not out:
            raise RouteFailed(f"fallback_any_model 下也没有任何可用候选，"
                              f"目标 {t} 无法满足", tried=[str(c) for c in cands])
        return out

    # ── 过滤 ──
    @staticmethod
    def _filter(cands: list[Candidate], t: ManualTarget) -> list[Candidate]:
        out = [c for c in cands if c.model == t.model]
        if t.provider is not None:
            out = [c for c in out if c.provider == t.provider]
        if t.account is not None:
            out = [c for c in out if c.account == t.account]
        return out

    @staticmethod
    def _same_model_others(cands: list[Candidate], t: ManualTarget) -> list[Candidate]:
        """同型号但**不同实例**（换家 / 换号）。"""
        return [c for c in cands
                if c.model == t.model
                and not (c.provider == t.provider and c.account == t.account)]

    @staticmethod
    def _cross_model(cands: list[Candidate], t: ManualTarget,
                     already: list[Candidate]) -> list[Candidate]:
        """跨型号候选（排在同型号之后）。"""
        seen = set(already)
        return [c for c in cands if c.model != t.model and c not in seen]
