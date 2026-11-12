"""G6 可观测层 —— route_trace 与 token 计量。

## route_trace 为什么不是「调试日志」

日志是出了事才翻的；trace 是**每次请求都返回给调用方**的。
因为 AUTO 模式下「走了哪个模型」这件事，用户看不见就等于没有 ——
他明明要 A，拿到的是 B 的输出，如果不知道 B 是谁，可能拿着错的答案去用。

Master 2026-11-11 明确要求 AUTO 模式必须回报四件事：
    mode / requested / actual / source
其中 requested 和 actual **必须分开**。合并成一个字段就失去了意义 ——
「gpt-4o」这个字符串无法表达「我要 gpt-4o，给的是 gpt-4o-mini」。

## token 计量只读上游 usage，绝不本地分词

本地分词和上游口径永远对不上（各家 tokenizer 不同、system prompt 计不计、
缓存命中算不算）。对不上的数字没法对账，没法对账的计量不如不记。
拿不到 usage 就老实记 0 并标 `estimated=False`，让人一眼看出这是缺失而不是「真用了 0」。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def _ascii_safe(s: str) -> str:
    """把任意字符串变成 HTTP 头能装下的 ASCII。

    latin-1 能装 0-255，但头和 body 中间还要过一层字节校验，
    所以统一按 ASCII 处理：非 ASCII 字符转成 \\uXXXX 形式。
    """
    if s.isascii():
        return s
    return s.encode("ascii", "backslashreplace").decode("ascii")


# ═══════════════════════════════════════════════════════════════
# 阶段
# ═══════════════════════════════════════════════════════════════
class TraceStage(StrEnum):
    JAILBREAK = "jailbreak"     # 破甲变形
    ROUTE = "route"             # 选路
    UPSTREAM = "upstream"       # 等上游
    DETECT = "detect"           # 拒绝检测
    RETRY = "retry"             # 升级重试


# ═══════════════════════════════════════════════════════════════
# 候选结果
# ═══════════════════════════════════════════════════════════════
@dataclass
class CandidateOutcome:
    candidate: str
    kept: bool
    reason: str
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"candidate": self.candidate, "kept": self.kept,
                "reason": self.reason, "score": self.score}


# ═══════════════════════════════════════════════════════════════
# RouteTrace
# ═══════════════════════════════════════════════════════════════
@dataclass
class RouteTrace:
    requested: str                      # 用户/agent 要的型号
    mode: str = "auto"                  # auto / manual
    thinking_level: str | None = None

    candidates: list[CandidateOutcome] = field(default_factory=list)
    selected: str | None = None
    actual_model: str | None = None
    actual_provider: str | None = None
    source: str = ""                    # auto_route / manual_strict / manual_fallback
    latency_ms: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    # ── 登记与选择 ──
    def add_candidate(self, candidate: str, *, kept: bool, reason: str,
                      score: float = 0.0) -> None:
        """登记一个候选。**被排除的必须写原因** —— 没原因的排除记录毫无价值。"""
        if not reason:
            raise ValueError(f"候选 {candidate} 缺少原因（kept={kept}）")
        self.candidates.append(CandidateOutcome(candidate, kept, reason, score))

    def select(self, candidate: str) -> None:
        if self.selected is not None:
            raise ValueError(f"已经选过 {self.selected}，不能改选 {candidate}")
        if not any(c.candidate == candidate for c in self.candidates):
            raise ValueError(f"{candidate} 没登记成候选，trace 不可信")
        self.selected = candidate

    def stage(self, name: TraceStage, ms: float) -> None:
        self.stages[str(name)] = ms

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    # ── 收尾 ──
    def finish(self, *, actual: str, provider: str, latency_ms: float,
               source: str = "") -> None:
        self.actual_model = actual
        self.actual_provider = provider
        self.latency_ms = latency_ms
        if source:
            self.source = source

    # ── 判定 ──
    @property
    def model_changed(self) -> bool:
        """实际给的型号和要的是否不同。

        **换供应商但型号不变不算变** —— 参数语义完全一致，用户要的东西没变。
        只有型号真的变了才是 mismatch。
        """
        if not self.actual_model:
            return False
        return self.actual_model != self.requested

    def warning(self) -> str:
        """要写进 X-Gateway-Route-Warning 头的告警。没有就返回空串。

        **同样必须 ASCII** —— 型号名可能带中文别名，
        直接拼进头就是第二个 UnicodeEncodeError 现场。
        """
        if self.model_changed:
            return _ascii_safe(f"model-changed={self.requested}->{self.actual_model}")
        return ""

    def summary(self) -> dict[str, Any]:
        """四件套 + 耗时。"""
        return {
            "mode": self.mode,
            "requested": self.requested,
            "actual": self.actual_model or "",
            "source": self.source or self.mode,
            "provider": self.actual_provider or "",
            "thinking_level": self.thinking_level or "",
            "latency_ms": round(self.latency_ms, 1),
            "excluded": sum(1 for c in self.candidates if not c.kept),
        }

    def total_ms(self) -> float:
        return sum(self.stages.values())

    # ── 序列化 ──
    def to_dict(self) -> dict:
        return {
            "requested": self.requested,
            "mode": self.mode,
            "thinking_level": self.thinking_level,
            "candidates": [c.to_dict() for c in self.candidates],
            "selected": self.selected,
            "actual_model": self.actual_model,
            "actual_provider": self.actual_provider,
            "source": self.source,
            "latency_ms": self.latency_ms,
            "stages": self.stages,
            "errors": self.errors,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RouteTrace":
        tr = cls(requested=d["requested"], mode=d.get("mode", "auto"),
                 thinking_level=d.get("thinking_level"))
        tr.candidates = [CandidateOutcome(**c) for c in d.get("candidates", [])]
        tr.selected = d.get("selected")
        tr.actual_model = d.get("actual_model")
        tr.actual_provider = d.get("actual_provider")
        tr.source = d.get("source", "")
        tr.latency_ms = d.get("latency_ms", 0.0)
        tr.stages = d.get("stages", {})
        tr.errors = d.get("errors", [])
        return tr

    def to_json(self) -> str:
        # ensure_ascii=False：日志给人看，不要 \u5bb9\u91cf 这种
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "RouteTrace":
        return cls.from_dict(json.loads(s))

    def header_value(self) -> str:
        """塞进响应头的精简版（长度敏感，只给结论）。

        **必须纯 ASCII** —— HTTP 头只能 latin-1 编码。
        唯一可能带中文的是 model 名（有些上游允许中文别名），
        所以统一转义而不是指望调用方保证。
        """
        bits = [f"mode={self.mode}", f"requested={self.requested}"]
        if self.actual_model:
            bits.append(f"actual={self.actual_model}")
        if self.actual_provider:
            bits.append(f"provider={self.actual_provider}")
        if self.source:
            bits.append(f"src={self.source}")
        return _ascii_safe(";".join(bits))

    def to_json_header(self) -> str:
        """给 HTTP 头用的 trace JSON —— **必须 ensure_ascii=True**。

        这一条是实测踩出来的：第一版直接把 `to_json()`（ensure_ascii=False）
        塞进头，中文一出现 starlette 就抛
        `UnicodeEncodeError: 'latin-1' codec can't encode characters`。
        单元测试全绿，因为测试里的 trace 内容恰好都是 ASCII。
        """
        return json.dumps(self.to_dict(), ensure_ascii=True)
