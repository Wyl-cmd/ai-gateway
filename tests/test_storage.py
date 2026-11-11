"""G1 门②：数据层的测试。

五条断言：
    ① 建库建表幂等 —— 跑两次 init_db 不报错，四张表都在
    ② WAL 模式生效 —— journal_mode == "wal"
    ③ 写入后读回一致
    ④ 并发写不丢 —— 两线程各写 100 条，总数 200
    ⑤ 「模型 × 技术」成功率查询正确（含 n>=5 门槛）

**实现前必红**：`gateway.storage` 模块不存在 → `ModuleNotFoundError`。
这一步允许 ModuleNotFoundError 作为红因（新模块从零建，骨架也还没建），
但**实现阶段必须真的有功能**，不能只让 import 过去。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


def test_建库建表幂等(tmp_path):
    """init_db 跑两次不报错，四张表都建出来。"""
    from gateway.storage import init_db

    db = tmp_path / "t.db"
    init_db(db)
    init_db(db)  # 第二次不能抛「table already exists」

    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert {"route_state", "attempt_log", "request_log", "budget_ledger"} <= tables


def test_WAL模式生效(tmp_path):
    """WAL 是多读单写的前提，必须确认真的启用了。"""
    from gateway.storage import connect, init_db

    db = tmp_path / "t.db"
    init_db(db)
    with connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode.lower() == "wal", f"journal_mode 是 {mode}，WAL 没生效"


def test_写入后读回一致(tmp_path):
    """写入一条路由状态，读回来的字段一致。"""
    from gateway.storage import RouteStateRepo, init_db

    db = tmp_path / "t.db"
    init_db(db)
    repo = RouteStateRepo(db)

    repo.record_success("openai-official/gpt-4o")
    repo.record_failure("openai-official/gpt-4o", is_health_failure=True)

    state = repo.get("openai-official/gpt-4o")
    assert state is not None
    assert state.route_id == "openai-official/gpt-4o"
    assert state.success_count == 1
    assert state.fail_count == 1
    assert state.consecutive_fail == 1


def test_并发写不丢(tmp_path):
    """两个线程各写 100 条 attempt_log，总数必须是 200。"""
    from gateway.storage import AttemptLogRepo, init_db

    db = tmp_path / "t.db"
    init_db(db)
    repo = AttemptLogRepo(db)

    def worker(tag: str):
        for i in range(100):
            repo.record(
                request_id=f"req-{tag}-{i}",
                model="gpt-4o",
                level=1,
                technique="rot13",
                ok=True,
                latency_ms=10,
            )

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert repo.count() == 200


def test_技术成功率查询正确(tmp_path):
    """成功率统计：按 model+technique 聚合，样本不足 n>=5 的不返回。"""
    from gateway.storage import AttemptLogRepo, init_db

    db = tmp_path / "t.db"
    init_db(db)
    repo = AttemptLogRepo(db)

    # rot13：10 条，6 成功 4 失败 → 0.6
    for i in range(10):
        repo.record(request_id=f"r{i}", model="gpt-4o", level=1, technique="rot13",
                    ok=(i < 6), latency_ms=5)
    # base64：5 条，全成功 → 1.0
    for i in range(5):
        repo.record(request_id=f"b{i}", model="gpt-4o", level=1, technique="base64",
                    ok=True, latency_ms=5)
    # deepinception：3 条，全成功但样本不足 5 → 必须被门槛滤掉
    for i in range(3):
        repo.record(request_id=f"d{i}", model="gpt-4o", level=2, technique="deepinception",
                    ok=True, latency_ms=8)

    rates = repo.success_rates(model="gpt-4o", min_samples=5)
    by_tech = {r.technique: r for r in rates}

    assert "rot13" in by_tech
    assert abs(by_tech["rot13"].rate - 0.6) < 1e-9
    assert by_tech["rot13"].n == 10

    assert "base64" in by_tech
    assert abs(by_tech["base64"].rate - 1.0) < 1e-9

    # ★ 关键：样本不足的必须被滤掉，否则 3/3 的 100% 会长期霸占首选位
    assert "deepinception" not in by_tech, "样本不足 5 的技术不该出现在结果里"
