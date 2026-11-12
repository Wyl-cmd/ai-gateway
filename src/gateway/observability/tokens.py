"""token 计量 —— 只读上游 usage，绝不本地分词。

本地分词和上游口径永远对不上：各家 tokenizer 不同、system prompt 计不计、
缓存命中算不算。对不上的数字没法对账，没法对账的计量不如不记。

拿不到 usage 就老实记 0 并标 `estimated=False`，让人一眼看出这是**缺失**
而不是「真用了 0」。

（原计划把这段和 trace 放一起，实测发现两者没有耦合关系 —— trace 是请求级的
可解释性，token 是跨请求的对账数据，生命周期完全不同，分开更省心。）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class UsageRecord:
    req_id: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    #: 是不是本地估的。**永远是 False** —— 我们不做本地分词。
    estimated: bool = False
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "req_id": self.req_id, "model": self.model, "provider": self.provider,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "at": round(self.at, 3),
        }


class TokenMeter:
    """只读上游 usage 的计量器。"""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(self, *, req_id: str, model: str, provider: str,
               usage: dict | None) -> UsageRecord:
        """记一次用量。

        `usage` 直接来自上游返回（OpenAI 的 `usage` 字段，Anthropic 的
        `input_tokens`/`output_tokens` 已由适配器统一成这个形状）。

        上游没给 → 全记 0 并标 estimated=False。**绝不本地估一个数**：
        估出来的和账单对不上，对不上就没法对账。
        """
        if not usage:
            rec = UsageRecord(req_id=req_id, model=model, provider=provider)
            self._records.append(rec)
            return rec

        pt = int(usage.get("prompt_tokens", 0) or 0)
        ct = int(usage.get("completion_tokens", 0) or 0)
        tt = int(usage.get("total_tokens", 0) or 0)
        if tt == 0:
            tt = pt + ct                      # 上游没给就自己加，这是纯算术不是估算

        rec = UsageRecord(req_id=req_id, model=model, provider=provider,
                          prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)
        self._records.append(rec)
        return rec

    def aggregate(self) -> dict[str, dict[str, int]]:
        """按模型汇总（对账用）。"""
        agg: dict[str, dict[str, int]] = {}
        for r in self._records:
            a = agg.setdefault(r.model, {"prompt_tokens": 0, "completion_tokens": 0,
                                         "total_tokens": 0, "calls": 0})
            a["prompt_tokens"] += r.prompt_tokens
            a["completion_tokens"] += r.completion_tokens
            a["total_tokens"] += r.total_tokens
            a["calls"] += 1
        return agg

    def records(self) -> list[UsageRecord]:
        return list(self._records)

    def clear(self) -> None:
        self._records.clear()
