"""G6 可观测层测试（route_trace + token 计量）。

这一层只有一个目的：**让「为什么走了这条路」可复现、可追责**。

三个必须做到：
  ① 每个被排除的候选都有非空原因 —— 只有「排除了 N 个」而没有「因为什么」，
     等于没说。用户看到 auto 模式下走了个慢模型，有权知道为什么没走快的那个。
  ② trace 能 JSON 序列化再反序列化 —— 要塞进响应头、写进日志、存进 DB。
  ③ token 计量只读上游 usage，不本地分词 —— 本地方案和上游口径永远对不上，
     对不上就没法对账，没法对账的计量不如不记。
"""

from __future__ import annotations

import json

import pytest

from gateway.observability.trace import (
    CandidateOutcome,
    RouteTrace,
    TraceStage,
)
from gateway.observability.tokens import TokenMeter, UsageRecord


# ═══════════════════════════════════════════════════════════════
# ① RouteTrace：每个排除都要有原因
# ═══════════════════════════════════════════════════════════════
def test_被排除的候选必须有原因():
    tr = RouteTrace(requested="deepseek-chat", mode="auto")
    tr.add_candidate("openai/gpt-4o", kept=False, reason="provider 冷却中")
    tr.add_candidate("deepseek/key1/deepseek-chat", kept=True, reason="入选")

    for c in tr.candidates:
        assert c.reason, f"{c.candidate} 没写原因"


def test_空原因的排除会被拒绝():
    """宁可报错也不能静默记一条没原因的排除 —— 那条记录没有任何价值。"""
    tr = RouteTrace(requested="m", mode="auto")
    with pytest.raises(ValueError):
        tr.add_candidate("x/y", kept=False, reason="")


def test_最终选中要能取到():
    tr = RouteTrace(requested="deepseek-chat", mode="auto")
    tr.add_candidate("a", kept=True, reason="入选")
    tr.add_candidate("b", kept=True, reason="入选")
    tr.select("b")
    assert tr.selected == "b"


def test_选中的必须在候选里():
    """选了一个没登记过的候选 = trace 不可信。"""
    tr = RouteTrace(requested="m", mode="auto")
    tr.add_candidate("a", kept=True, reason="入选")
    with pytest.raises(ValueError):
        tr.select("不存在")


def test_不能重复选两个():
    tr = RouteTrace(requested="m", mode="auto")
    tr.add_candidate("a", kept=True, reason="入选")
    tr.add_candidate("b", kept=True, reason="入选")
    tr.select("a")
    with pytest.raises(ValueError):
        tr.select("b")


# ═══════════════════════════════════════════════════════════════
# ② 序列化往返
# ═══════════════════════════════════════════════════════════════
def test_trace_能_JSON_往返():
    tr = RouteTrace(requested="deepseek-chat", mode="auto", thinking_level="medium")
    tr.add_candidate("p1/k1/m", kept=True, reason="入选", score=0.87)
    tr.add_candidate("p2/k1/m", kept=False, reason="账号配额耗尽", score=0.0)
    tr.select("p1/k1/m")
    tr.finish(actual="deepseek-chat", provider="p1", latency_ms=320)

    s = tr.to_json()
    tr2 = RouteTrace.from_json(s)

    assert tr2.requested == tr.requested
    assert tr2.mode == tr.mode
    assert tr2.selected == tr.selected
    assert tr2.actual_model == tr.actual_model
    assert len(tr2.candidates) == 2
    assert tr2.candidates[1].reason == "账号配额耗尽"


def test_JSON_里中文不转义():
    """日志给人看，不要 \\u5bb9\\u91cf 这种。"""
    tr = RouteTrace(requested="m", mode="auto")
    tr.add_candidate("a", kept=False, reason="账号配额耗尽")
    assert "账号配额耗尽" in tr.to_json()


def test_响应头用的精简版不含候选细节():
    """响应头有长度限制，只给结论不给全量候选。"""
    tr = RouteTrace(requested="deepseek-chat", mode="auto")
    tr.add_candidate("a", kept=True, reason="入选")
    tr.select("a")
    tr.finish(actual="deepseek-chat", provider="deepseek", latency_ms=100)

    hdr = tr.header_value()
    assert "auto" in hdr
    assert "deepseek-chat" in hdr
    assert len(hdr) < 200


# ═══════════════════════════════════════════════════════════════
# ③ AUTO 模式四件套（Master 2026-11-11 明确要求）
# ═══════════════════════════════════════════════════════════════
def test_auto_模式必须回报四件事():
    """模式 / requested / actual / source 一个都不能少。
    用户必须能区分「我要的」和「实际给的」。"""
    tr = RouteTrace(requested="deepseek-chat", mode="auto")
    tr.add_candidate("a", kept=True, reason="入选")
    tr.select("a")
    tr.finish(actual="deepseek-v3", provider="deepseek", latency_ms=100,
              source="auto_route")

    d = tr.summary()
    for k in ("mode", "requested", "actual", "source"):
        assert d.get(k), f"缺 {k}"


def test_manual_fallback_时型号变了要标记():
    """manual 模式下发生降级 = 用户指定的东西没给到，这是最需要提示的情况。"""
    tr = RouteTrace(requested="gpt-4o", mode="manual")
    tr.add_candidate("openai/gpt-4o", kept=False, reason="provider 冷却中")
    tr.add_candidate("azure/gpt-4o-mini", kept=True, reason="型号降级")
    tr.select("azure/gpt-4o-mini")
    tr.finish(actual="gpt-4o-mini", provider="azure", latency_ms=200,
              source="manual_fallback")

    assert tr.model_changed is True
    assert "gpt-4o" in tr.warning() and "gpt-4o-mini" in tr.warning()


def test_换供应商但型号不变不算_mismatch():
    """同型号换家不算降级 —— 参数语义完全一致，用户要的东西没变。"""
    tr = RouteTrace(requested="deepseek-chat", mode="auto")
    tr.add_candidate("backup/deepseek-chat", kept=True, reason="入选")
    tr.select("backup/deepseek-chat")
    tr.finish(actual="deepseek-chat", provider="backup", latency_ms=200,
              source="auto_route")
    assert tr.model_changed is False
    assert tr.warning() == ""


# ═══════════════════════════════════════════════════════════════
# ④ 阶段记录
# ═══════════════════════════════════════════════════════════════
def test_能记录各阶段耗时():
    tr = RouteTrace(requested="m", mode="auto")
    tr.stage(TraceStage.JAILBREAK, 12.5)
    tr.stage(TraceStage.ROUTE, 3.1)
    tr.stage(TraceStage.UPSTREAM, 890.0)
    assert tr.stages["jailbreak"] == 12.5
    assert tr.total_ms() > 900


# ═══════════════════════════════════════════════════════════════
# ⑤ token 计量：只读上游 usage
# ═══════════════════════════════════════════════════════════════
def test_计量读数与上游_usage_一致():
    m = TokenMeter()
    usage = {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
    rec = m.record(req_id="r1", model="deepseek-chat",
                   provider="deepseek", usage=usage)
    assert rec.prompt_tokens == 120
    assert rec.completion_tokens == 45
    assert rec.total_tokens == 165


def test_上游没给_total_时自己加():
    m = TokenMeter()
    rec = m.record(req_id="r1", model="m", provider="p",
                   usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert rec.total_tokens == 15


def test_上游完全没给_usage_时记零不是猜():
    """拿不到 usage 就老实记 0，不要本地分词估一个数。
    估出来的数和上游账单对不上，对不上就没法对账。"""
    m = TokenMeter()
    rec = m.record(req_id="r1", model="m", provider="p", usage=None)
    assert rec.prompt_tokens == 0
    assert rec.completion_tokens == 0
    assert rec.estimated is False


def test_累计统计按模型分组():
    m = TokenMeter()
    m.record(req_id="r1", model="a", provider="p", usage={"prompt_tokens": 10, "completion_tokens": 5})
    m.record(req_id="r2", model="a", provider="p", usage={"prompt_tokens": 20, "completion_tokens": 8})
    m.record(req_id="r3", model="b", provider="p", usage={"prompt_tokens": 30, "completion_tokens": 2})

    agg = m.aggregate()
    assert agg["a"]["prompt_tokens"] == 30
    assert agg["a"]["completion_tokens"] == 13
    assert agg["b"]["prompt_tokens"] == 30


def test_计量记录可序列化():
    m = TokenMeter()
    rec = m.record(req_id="r1", model="m", provider="p",
                   usage={"prompt_tokens": 1, "completion_tokens": 2})
    d = rec.to_dict()
    assert json.loads(json.dumps(d)) == d
