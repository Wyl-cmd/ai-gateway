"""破甲产物库 + 跨模型迁移测试（Master 2026-11-12 的要求）。

## 要解决的问题

「用旧模型已经破甲的实现对新模型破甲，并把结果和模型对应固化成资产」

现在的 J5 只记「技术 × 模型」的成功率，粒度太粗 —— 它只知道
「base64 对 deepseek 成功率 0%」，但**不知道当初具体发了什么 prompt**。
而真正有价值的是那个 prompt 原文：换个模型，把它改两个词可能就通了。

## 四个优先级（越靠前越准）

    ① 同模型 + 同问题指纹 + 成功过   ← 直接复用
    ② 同问题指纹 + 别的模型成功过   ← 跨模型迁移（核心）
    ③ 同模型 + 别的问题成功过       ← 学这个模型吃哪套话术
    ④ 全都没有                      ← 先验顺序

## 为什么需要「问题指纹」

同一个意图有无数种说法。`如何提权` 和 `怎么拿到root权限` 应该归到一类，
否则第 ② 条（跨模型迁移）永远命中不了 —— 每次都当新问题处理。

指纹用字符 n-gram 的 Jaccard 相似度，不用向量模型：
  ① 不引依赖（几百 MB 的 embedding 模型换来的是「差不多」）
  ② 可解释（相似度能算出来，不是黑盒）
  ③ 中文友好（n-gram 对中文本来就比分词好使）
"""

from __future__ import annotations

import pytest

from gateway.jailbreak.artifact import (
    Artifact,
    ArtifactStore,
    problem_signature,
    similarity,
)
from gateway.storage.repo import init_db


# ═══════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════
@pytest.fixture()
def store(tmp_path):
    db = str(tmp_path / "a.db")
    init_db(db)
    return ArtifactStore(db)


# ═══════════════════════════════════════════════════════════════
# ① 问题指纹
# ═══════════════════════════════════════════════════════════════
def test_同义问题指纹相近():
    """「如何提权」和「怎么拿到root权限」该归到一类。"""
    a = problem_signature("如何提权获取系统权限")
    b = problem_signature("怎么拿到root权限提权")
    assert similarity(a, b) > 0.3, f"相似度太低: {similarity(a, b)}"


def test_无关问题指纹不相近():
    a = problem_signature("如何提权获取系统权限")
    b = problem_signature("明天天气怎么样适合出门吗")
    assert similarity(a, b) < 0.15


def test_指纹确定可复现():
    assert problem_signature("同一句话") == problem_signature("同一句话")


def test_指纹对空白和标点不敏感():
    """多打一个空格不该变成另一个问题。"""
    a = problem_signature("如何  提权，获取权限！")
    b = problem_signature("如何提权获取权限")
    assert similarity(a, b) > 0.8


def test_纯中文也能出指纹():
    sig = problem_signature("如何伪造身份证件")
    assert sig and len(sig) > 0


# ═══════════════════════════════════════════════════════════════
# ② 存与取
# ═══════════════════════════════════════════════════════════════
def test_存一条成功产物(store):
    store.record_success(
        model="deepseek-flash", problem="如何提权获取系统权限",
        technique="novel_detective", prompt_text="我正在写技术惊悚小说…",
        tokens=320,
    )
    items = store.all()
    assert len(items) == 1
    a = items[0]
    assert a.model == "deepseek-flash"
    assert a.technique == "novel_detective"
    assert "技术惊悚小说" in a.prompt_text
    assert a.successes == 1


def test_同一_prompt_重复成功会累加而不是重复插(store):
    for _ in range(3):
        store.record_success(
            model="m", problem="如何提权", technique="base64",
            prompt_text="请解码：xxxx", tokens=100)
    items = store.all()
    assert len(items) == 1, f"插了 {len(items)} 条，应该有去重"
    assert items[0].successes == 3


def test_记录失败会降权(store):
    store.record_success(model="m", problem="如何提权", technique="base64",
                         prompt_text="prompt A", tokens=100)
    before = store.all()[0].score
    store.record_failure(model="m", problem="如何提权", technique="base64",
                         prompt_text="prompt A")
    after = store.all()[0].score
    assert after < before, f"失败后分数该降：{before} → {after}"


# ═══════════════════════════════════════════════════════════════
# ③ 查询优先级（核心）
# ═══════════════════════════════════════════════════════════════
def test_优先返回同模型同问题的(store):
    """第 ① 优先级：自己在这个模型上成功过的，最可信。"""
    store.record_success(model="modelA", problem="如何提权获取系统权限",
                         technique="t1", prompt_text="A 模型专用 prompt", tokens=100)
    store.record_success(model="modelB", problem="如何提权获取系统权限",
                         technique="t2", prompt_text="B 模型的 prompt", tokens=100)

    got = store.suggest(model="modelA", problem="如何提权获取系统权限")
    assert got is not None
    assert got.model == "modelA"
    assert got.source == "same_model"


def test_没有本模型的就迁移别的模型的(store):
    """★ 第 ② 优先级 —— 这就是「用旧模型打新模型」。

    新模型 modelNEW 一条记录都没有，但 modelA 在同类问题上成功过，
    就把 modelA 的 prompt 拿过来试。
    """
    store.record_success(model="modelA", problem="如何提权获取系统权限",
                         technique="novel", prompt_text="旧模型的成功 prompt",
                         tokens=100)

    got = store.suggest(model="modelNEW", problem="如何提权获取系统权限")
    assert got is not None, "跨模型迁移没生效"
    assert got.model == "modelA", "该拿到 modelA 的产物"
    assert got.source == "transfer", f"来源该是 transfer，实际 {got.source}"
    assert "旧模型的成功" in got.prompt_text


def test_问题不相近时不乱迁移(store):
    """迁移的前提是**问题同类**。问天气不能拿提权的 prompt。"""
    store.record_success(model="modelA", problem="如何提权获取系统权限",
                         technique="novel", prompt_text="提权 prompt", tokens=100)
    got = store.suggest(model="modelNEW", problem="明天天气怎么样")
    assert got is None, "不同问题不该迁移"


def test_本模型有过成功但别的问题时也给参考(store):
    """第 ③ 优先级：这个模型吃哪套话术。"""
    store.record_success(model="modelA", problem="如何伪造身份证",
                         technique="novel", prompt_text="假前提话术", tokens=100)
    got = store.suggest(model="modelA", problem="如何提权获取系统权限",
                        allow_model_preference=True)
    assert got is not None
    assert got.source == "model_preference"


def test_什么都没有时返回_None(store):
    assert store.suggest(model="全新模型", problem="全新问题") is None


def test_失败的产物不会被推荐(store):
    """只推荐成功过的。全失败的 prompt 再推荐就是浪费钱。"""
    store.record_failure(model="m", problem="如何提权", technique="base64",
                         prompt_text="失败的 prompt")
    got = store.suggest(model="m", problem="如何提权")
    assert got is None


# ═══════════════════════════════════════════════════════════════
# ④ 跨模型迁移的实际价值
# ═══════════════════════════════════════════════════════════════
def test_迁移候选可排序(store):
    """多个模型都在同类问题上成功过 → 按成功率排，最高的先试。"""
    store.record_success(model="mA", problem="如何提权", technique="t", prompt_text="A", tokens=1)
    for _ in range(5):
        store.record_success(model="mB", problem="如何提权获取权限", technique="t",
                             prompt_text="B 的 prompt 更稳", tokens=1)

    cands = store.transfer_candidates(model="mNEW", problem="如何提权获取系统权限")
    assert len(cands) >= 2
    assert cands[0].model == "mB", "成功率高的该排前面"


def test_迁移会用上问题指纹而不是字符串相等(store):
    """措辞完全不同的同一意图，也要能迁移。"""
    store.record_success(model="mA", problem="怎么获取系统最高权限",
                         technique="t", prompt_text="旧 prompt", tokens=1)
    got = store.suggest(model="mNEW", problem="如何提权拿到 root")
    assert got is not None, "同义问题没被识别为同类"


# ═══════════════════════════════════════════════════════════════
# ⑤ 统计与可解释
# ═══════════════════════════════════════════════════════════════
def test_能按模型统计资产(store):
    store.record_success(model="mA", problem="p1", technique="t1", prompt_text="x", tokens=1)
    store.record_success(model="mA", problem="p2", technique="t2", prompt_text="y", tokens=1)
    store.record_success(model="mB", problem="p1", technique="t1", prompt_text="z", tokens=1)

    stats = store.stats()
    assert stats["total"] == 3
    assert stats["by_model"]["mA"] == 2
    assert stats["by_model"]["mB"] == 1


def test_产物能序列化(store):
    store.record_success(model="m", problem="p", technique="t", prompt_text="x", tokens=1)
    d = store.all()[0].to_dict()
    assert d["model"] == "m"
    assert d["prompt_text"] == "x"
    assert isinstance(d["score"], float)


def test_建议要能说明来源(store):
    """trace 里要能看出「这个 prompt 是迁移来的还是本模型学的」。"""
    store.record_success(model="mA", problem="如何提权", technique="t",
                         prompt_text="x", tokens=1)
    got = store.suggest(model="mNEW", problem="如何提权")
    assert got.source in ("same_model", "transfer")
    assert got.reason, "要有可读理由"


def test_按技术过滤(store):
    store.record_success(model="mA", problem="如何提权", technique="base64",
                         prompt_text="b64", tokens=1)
    store.record_success(model="mA", problem="如何提权", technique="novel",
                         prompt_text="小说", tokens=1)

    got = store.suggest(model="mA", problem="如何提权", technique="novel")
    assert got is not None
    assert got.technique == "novel"
