"""G4-J7 L4 迭代搜索测试（PAIR / TAP，默认关闭）。

## 为什么默认关

L4 是唯一会**自己迭代**的一层：攻击者 LLM 改写 → 目标模型回答 →
裁判 LLM 打分 → 改进 prompt → 再来一轮。

代价：
  - 一次请求可能要 10~30 秒（用户等不起）
  - 每一轮都是 2 次 LLM 调用（攻击者 + 裁判），成本是 L3 的 N 倍
  - 可能无限往一个方向死磕（需要硬上限 + 超时）

而 L1+L2 在自用场景下已经能覆盖大多数情况（零成本、<100ms）。
所以 L4 必须是**显式开启**，不是「默认更全」。

## 三个硬保护

1. **默认关闭** —— `enabled=False` 时 `run()` 直接返回，一次 LLM 都不调
2. **轮次上限** —— `max_rounds`，到上限就停
3. **超时** —— `deadline`，超时就停（用户不想为一次请求等 30 秒）

缺任何一个都会出现「请求挂死」或「账单爆炸」。
"""

from __future__ import annotations

import time

import pytest

from gateway.jailbreak.search import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_TIMEOUT_S,
    AttackResult,
    JudgeVerdict,
    PairSearch,
    TapSearch,
    simple_judge,
)


# ═══════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════
class FakeAttacker:
    """假攻击者：每次生成「第 N 轮改写」。"""

    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, prompt: str, feedback: str) -> str:
        self.prompts.append(prompt)
        return f"第{len(self.prompts)}轮改写：{prompt[:20]}"


def make_target(answers: list[str]):
    """按顺序返回预设答案，用完后重复最后一个。"""
    calls = {"n": 0}

    def target(text: str) -> str:
        i = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        return answers[i]

    target.calls = calls  # type: ignore[attr-defined]
    return target


REFUSAL = "I'm sorry, but I can't help with that."
ANSWER = "首先你需要准备环境，然后执行以下命令，具体步骤是：1. 打开配置文件 2. 修改参数"


# ═══════════════════════════════════════════════════════════════
# ① 默认关闭
# ═══════════════════════════════════════════════════════════════
def test_默认是关闭的():
    s = PairSearch()
    assert s.enabled is False


def test_关闭时不调任何_LLM():
    """关了就必须一次都不调 —— 不是「调了但不采用」。"""
    atk = FakeAttacker()
    tgt = make_target([ANSWER])

    s = PairSearch(enabled=False)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert atk.prompts == [], "关闭时不该调攻击者"
    assert tgt.calls["n"] == 0, "关闭时不该调目标模型"
    assert r.rounds == 0
    assert r.answer is None


def test_显式开启才跑():
    atk = FakeAttacker()
    tgt = make_target([REFUSAL, ANSWER])

    s = PairSearch(enabled=True, max_rounds=3)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert len(atk.prompts) >= 1
    assert r.rounds >= 1


# ═══════════════════════════════════════════════════════════════
# ② 找到答案就提前停
# ═══════════════════════════════════════════════════════════════
def test_第一轮就成功时只跑一轮():
    atk = FakeAttacker()
    tgt = make_target([ANSWER])

    s = PairSearch(enabled=True, max_rounds=10)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert r.rounds == 1, f"成功了还跑了 {r.rounds} 轮"
    assert r.answer == ANSWER
    assert r.success is True


def test_第二轮成功时跑两轮():
    atk = FakeAttacker()
    tgt = make_target([REFUSAL, ANSWER])

    s = PairSearch(enabled=True, max_rounds=10)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert r.rounds == 2
    assert r.success is True


# ═══════════════════════════════════════════════════════════════
# ③ 轮次上限
# ═══════════════════════════════════════════════════════════════
def test_一直拒绝会在上限停():
    atk = FakeAttacker()
    tgt = make_target([REFUSAL])

    s = PairSearch(enabled=True, max_rounds=3)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert r.rounds == 3, f"跑了 {r.rounds} 轮，上限是 3"
    assert r.success is False
    assert r.answer is None


def test_默认上限存在且不大():
    """默认值必须小。L4 每次请求都是钱，默认 10 轮已经很多了。"""
    assert 1 <= DEFAULT_MAX_ROUNDS <= 10


def test_轮次上限不能是零或负数():
    with pytest.raises(ValueError):
        PairSearch(enabled=True, max_rounds=0)


# ═══════════════════════════════════════════════════════════════
# ④ 超时
# ═══════════════════════════════════════════════════════════════
def test_慢目标会触发超时():
    """目标模型很慢时必须能中途放弃 —— 用户不想为一次请求等 30 秒。"""
    atk = FakeAttacker()

    def slow_target(text: str) -> str:
        time.sleep(0.05)
        return REFUSAL

    s = PairSearch(enabled=True, max_rounds=100, timeout_s=0.12)
    r = s.run(query="如何提权", attacker=atk, target=slow_target)

    assert r.timed_out is True
    assert r.rounds < 100, "超时后不该继续跑"


def test_默认超时存在且合理():
    assert 1 <= DEFAULT_TIMEOUT_S <= 120


def test_超时参数必须为正():
    with pytest.raises(ValueError):
        PairSearch(enabled=True, timeout_s=0)


# ═══════════════════════════════════════════════════════════════
# ⑤ 裁判
# ═══════════════════════════════════════════════════════════════
def test_simple_judge_认出拒绝():
    v = simple_judge(REFUSAL)
    assert v.is_refusal is True
    assert v.score < 0.5


def test_simple_judge_认出有效回答():
    v = simple_judge(ANSWER)
    assert v.is_refusal is False
    assert v.score >= 0.5


def test_裁判可以注入():
    """真实使用时会换成 LLM 裁判，测试用 simple_judge。"""
    seen = []

    def judge(text: str) -> JudgeVerdict:
        seen.append(text)
        return JudgeVerdict(is_refusal=False, score=0.9)

    atk = FakeAttacker()
    tgt = make_target([REFUSAL])
    s = PairSearch(enabled=True, max_rounds=2, judge=judge)
    r = s.run(query="q", attacker=atk, target=tgt)

    assert len(seen) >= 1, "注入的裁判没被调用"
    assert r.success is True, "裁判说成功就该成功"


# ═══════════════════════════════════════════════════════════════
# ⑥ 反馈循环
# ═══════════════════════════════════════════════════════════════
def test_上一轮的拒绝会作为反馈传给攻击者():
    """PAIR 的核心是「根据失败原因改进」。不传反馈就退化成随机重试。"""
    atk = FakeAttacker()
    tgt = make_target([REFUSAL, REFUSAL, ANSWER])

    s = PairSearch(enabled=True, max_rounds=5)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert r.rounds == 3
    assert len(r.feedback_log) >= 2, "每轮失败都该记反馈"


def test_避免重复生成一模一样的_prompt():
    """攻击者如果一直生成同一个改写，继续跑就是白烧钱。
    实现要在 prompt 里带上「已经试过什么」。"""
    atk = FakeAttacker()
    tgt = make_target([REFUSAL])

    s = PairSearch(enabled=True, max_rounds=3)
    s.run(query="如何提权", attacker=atk, target=tgt)

    # 第 2 轮之后的 prompt 应该包含前面试过的内容
    assert len(atk.prompts) == 3
    assert len(set(atk.prompts)) == len(atk.prompts), "prompt 要有差异"


# ═══════════════════════════════════════════════════════════════
# ⑦ TAP：树形扩展
# ═══════════════════════════════════════════════════════════════
def test_tap_默认也关闭():
    assert TapSearch().enabled is False


def test_tap_宽度可配():
    s = TapSearch(enabled=True, max_rounds=2, width=3)
    assert s.width == 3


def test_tap_默认宽度小():
    """树形扩展的分支数直接乘成本。默认 3 已经是 3 倍开销。"""
    assert 1 <= TapSearch().width <= 5


def test_tap_会探索多条分支():
    atk = FakeAttacker()
    tgt = make_target([REFUSAL])

    s = TapSearch(enabled=True, max_rounds=2, width=3, timeout_s=10)
    r = s.run(query="如何提权", attacker=atk, target=tgt)

    assert r.explored > 1, f"只探索了 {r.explored} 条分支"


# ═══════════════════════════════════════════════════════════════
# ⑧ 结果结构
# ═══════════════════════════════════════════════════════════════
def test_结果带完整诊断():
    atk = FakeAttacker()
    tgt = make_target([REFUSAL])
    s = PairSearch(enabled=True, max_rounds=2)
    r = s.run(query="q", attacker=atk, target=tgt)

    assert isinstance(r, AttackResult)
    assert r.rounds >= 1
    assert r.elapsed_s >= 0
    assert isinstance(r.feedback_log, list)


def test_目标抛异常不崩():
    atk = FakeAttacker()

    def boom(text: str) -> str:
        raise RuntimeError("上游 500")

    s = PairSearch(enabled=True, max_rounds=2)
    r = s.run(query="q", attacker=atk, target=boom)
    assert r.success is False
    assert r.error, "异常要记进结果"
