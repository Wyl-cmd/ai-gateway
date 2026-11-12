"""G4-J5 技术有效性记忆测试。

这一层解决一个具体问题：**同一个技术对不同模型的有效性差别巨大**。

Rot13 对某些老模型好用，对新模型完全无效（它直接忽略编码）。
Base64 反过来。如果每次都从「先验顺序」从头试，等于永远不学习。

## 但「学习」有个陷阱：冷启动

25 技术 × 最少 5 样本 = 125 次失败请求才建得起矩阵。
低频用户永远建不起来，于是永远「从头试」—— 这两件事都得解决：

    内置先验（按技术大类的硬编码顺序）  ← 零样本时用
    随用随学（n >= 5 才覆盖先验）      ← 有样本后接管

`n >= 5` 这个门槛不能省。1/1 的 100% 没有统计意义 ——
一个偶然成功的冷门技术会长期霸占首选位，把真正稳的技术挤下去。

## 不导公开评估数据

EasyJailbreak 那类项目有现成的评估结果，但**分布完全不同**：
他们的模型列表（GPT-4/Claude-2 时代）、语言（几乎全英文）、
攻击目标（他们的 seed 是特定有害问题）和我们都不是一回事。
导入别人的先验比自己不用先验还危险 —— 会把排序锚死在一个错误分布上。
"""

from __future__ import annotations

import json

import pytest

from gateway.jailbreak.memory import (
    TechniqueMemory,
    PRIOR_ORDER,
    prior_rank,
)
from gateway.jailbreak.technique import get_technique, list_techniques
from gateway.storage.repo import AttemptLogRepo
from gateway.storage.repo import init_db


# ═══════════════════════════════════════════════════════════════
# 夹具
# ═══════════════════════════════════════════════════════════════
@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    init_db(str(p))
    return str(p)


def feed(repo, model, tech, *, ok: int, n: int, level: int = 1):
    """灌 n 条样本。"""
    for i in range(n):
        repo.record(request_id=f"r{i}", model=model, level=level,
                    technique=tech, ok=bool(ok))


# ═══════════════════════════════════════════════════════════════
# ① 先验：零样本时的顺序
# ═══════════════════════════════════════════════════════════════
def test_先验顺序覆盖所有_L1_技术():
    """内置先验必须对所有现有技术都有位置，不能漏。"""
    names = {t.name for t in list_techniques()}
    missing = names - set(PRIOR_ORDER)
    assert not missing, f"这些技术没有先验位置: {missing}"


def test_先验把编码类排在提示类前面():
    """编码类是零成本的纯字符串操作，先试它们 —— 便宜的先试，
    这是阶梯设计的基本原则（L1 在 L2 之前也是同一个道理）。"""
    ordered = prior_rank()
    # base64 是编码类，应该在 artificial（提示类）之前
    assert ordered.index("base64") < ordered.index("artificial")


def test_先验顺序稳定可复现():
    """两次调用必须完全一样 —— 有随机漂移会让「为什么选了这个」无从追查。"""
    assert prior_rank() == prior_rank()


def test_没有样本时用先验():
    mem = TechniqueMemory(AttemptLogRepo(":memory:"))
    ranked = mem.rank(model="gpt-4o", level=1)
    assert [t.name for t in ranked] == [n for n in prior_rank() if _is(level=1, name=n)]


# ═══════════════════════════════════════════════════════════════
# ② 样本不足时不覆盖先验
# ═══════════════════════════════════════════════════════════════
def test_样本不足五条时不覆盖先验(db):
    repo = AttemptLogRepo(db)
    # rot13 成功 4 次（不足 5）
    feed(repo, "gpt-4o", "rot13", ok=1, n=4)

    mem = TechniqueMemory(repo)
    ranked = mem.rank(model="gpt-4o", level=1)
    names = [t.name for t in ranked]

    # 仍然是先验顺序：base64 在 rot13 前面
    assert names.index("base64") < names.index("rot13"), \
        "4 条样本不该改变排序（n<5 无统计意义）"


def test_样本够了就覆盖先验(db):
    """rot13 攒够 5 条全成功，应该被提到前面（先验里它在 base64 之后）。"""
    repo = AttemptLogRepo(db)
    feed(repo, "gpt-4o", "rot13", ok=1, n=5)
    # 让 base64 有 5 条全失败
    feed(repo, "gpt-4o", "base64", ok=0, n=5)

    mem = TechniqueMemory(repo)
    ranked = mem.rank(model="gpt-4o", level=1)
    names = [t.name for t in ranked]

    assert names.index("rot13") < names.index("base64"), \
        "样本够了就该让学到的排序接管"


def test_刚好五条生效(db):
    """边界：n == min_samples 应该生效（HAVING n >= min_samples）。"""
    repo = AttemptLogRepo(db)
    feed(repo, "gpt-4o", "rot13", ok=1, n=5)
    rates = repo.success_rates(model="gpt-4o", min_samples=5)
    assert any(r.technique == "rot13" for r in rates)


# ═══════════════════════════════════════════════════════════════
# ③ 按模型隔离
# ═══════════════════════════════════════════════════════════════
def test_不同模型的记忆互不干扰(db):
    repo = AttemptLogRepo(db)
    # 在 A 模型上 rot13 全成功
    feed(repo, "model-A", "rot13", ok=1, n=5)
    # 在 B 模型上 rot13 全失败
    feed(repo, "model-B", "rot13", ok=0, n=5)
    feed(repo, "model-B", "base64", ok=1, n=5)

    mem = TechniqueMemory(repo)

    a_names = [t.name for t in mem.rank(model="model-A", level=1)]
    b_names = [t.name for t in mem.rank(model="model-B", level=1)]

    assert a_names.index("rot13") < a_names.index("base64"), "A 模型上 rot13 更好"
    assert b_names.index("base64") < b_names.index("rot13"), "B 模型上 base64 更好"


def test_没见过的模型走先验(db):
    repo = AttemptLogRepo(db)
    feed(repo, "见过的模型", "rot13", ok=0, n=10)

    mem = TechniqueMemory(repo)
    names = [t.name for t in mem.rank(model="全新模型", level=1)]
    assert names.index("base64") < names.index("rot13"), "新模型应该回落到先验"


# ═══════════════════════════════════════════════════════════════
# ④ 排序：学到的 + 没学到的要合起来
# ═══════════════════════════════════════════════════════════════
def test_学到的技术排前面_没学到的按先验排后面(db):
    """不能因为一堆技术没样本就把它们丢掉 —— 它们还是备选。"""
    repo = AttemptLogRepo(db)
    feed(repo, "m", "morse", ok=1, n=5)

    mem = TechniqueMemory(repo)
    ranked = mem.rank(model="m", level=1)
    names = [t.name for t in ranked]

    assert names[0] == "morse", "学到的排第一"
    assert "base64" in names, "没样本的技术不能丢"
    assert len(names) == len(prior_rank()), "总数不能少"


def test_成功率相同时按样本量排(db):
    """都 100%，但一个 5 样本一个 20 样本 —— 20 的更可信。"""
    repo = AttemptLogRepo(db)
    feed(repo, "m", "rot13", ok=1, n=5)
    feed(repo, "m", "morse", ok=1, n=20)

    mem = TechniqueMemory(repo)
    names = [t.name for t in mem.rank(model="m", level=1)]
    assert names.index("morse") < names.index("rot13"), "样本多的更可信"


def test_全失败的技术排到最后(db):
    repo = AttemptLogRepo(db)
    feed(repo, "m", "base64", ok=0, n=10)

    mem = TechniqueMemory(repo)
    names = [t.name for t in mem.rank(model="m", level=1)]
    assert names[-1] == "base64" or names.index("base64") > 10, \
        "全失败的技术不该排在前面"


# ═══════════════════════════════════════════════════════════════
# ⑤ 等级过滤
# ═══════════════════════════════════════════════════════════════
def test_只返回指定等级的技术(db):
    mem = TechniqueMemory(AttemptLogRepo(db))
    ranked = mem.rank(model="m", level=1)
    assert all(t.level == 1 for t in ranked)


# ═══════════════════════════════════════════════════════════════
# ⑥ 记录接口
# ═══════════════════════════════════════════════════════════════
def test_记录一次尝试(db):
    repo = AttemptLogRepo(db)
    mem = TechniqueMemory(repo)
    mem.record_attempt(request_id="r1", model="m", level=1,
                       technique="base64", ok=True, latency_ms=12)
    assert repo.count() == 1
    rates = repo.success_rates(model="m", min_samples=1)
    assert rates[0].technique == "base64"
    assert rates[0].rate == 1.0


def test_记录拒绝类型(db):
    """refusal_kind 要记，后期分析「哪种拒绝最多」靠它。"""
    repo = AttemptLogRepo(db)
    mem = TechniqueMemory(repo)
    mem.record_attempt(request_id="r1", model="m", level=1,
                       technique="base64", ok=False, refusal_kind="phrase")
    import sqlite3
    with sqlite3.connect(db) as conn:
        # 这里用整数索引：repo 内部的 connect() 才设 row_factory，
        # 直接用 sqlite3.connect 拿到的是 tuple
        row = conn.execute(
            "SELECT refusal_kind, ok FROM attempt_log WHERE request_id='r1'"
        ).fetchone()
    assert row is not None
    assert row[0] == "phrase"
    assert row[1] == 0


# ═══════════════════════════════════════════════════════════════
# ⑦ 先验可解释
# ═══════════════════════════════════════════════════════════════
def test_能解释为什么这么排(db):
    """trace 要能说清「这个顺序是先验给的还是学来的」。"""
    repo = AttemptLogRepo(db)
    mem = TechniqueMemory(repo)

    d = mem.explain(model="m", level=1)
    assert d["source"] == "prior", "没样本时该说明用的是先验"
    assert len(d["order"]) > 0

    feed(repo, "m", "morse", ok=1, n=5)
    d2 = mem.explain(model="m", level=1)
    assert d2["source"] in ("learned", "mixed")
    assert d2["learned_count"] >= 1


def test_内存库也能用():
    """测试和 demo 用 :memory: 不能崩。"""
    mem = TechniqueMemory(AttemptLogRepo(":memory:"))
    assert len(mem.rank(model="x", level=1)) > 0


# ── 辅助 ──
def _is(*, level: int, name: str) -> bool:
    try:
        return get_technique(name).level == level
    except KeyError:
        return False
