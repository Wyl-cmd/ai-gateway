"""数据访问层：只做 CRUD，不做业务判断。

**为什么每次操作都新建连接**：sqlite3 的连接默认不能跨线程共享，
而网关是 asyncio + 线程池混跑。每次操作开一个短连接最简单也最安全；
SQLite 的打开成本极低（毫秒级以下），而 WAL 模式下读不阻塞写。

术语：
    WAL（Write-Ahead Logging，预写日志）—— 多读单写，读不阻塞写
    idempotent（幂等）—— 重复执行结果相同
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_SCHEMA = Path(__file__).with_name("schema.sql")

# 写等待上限（秒）。并发写时另一线程持有写锁，靠这个等而不是立刻报 database is locked
_BUSY_TIMEOUT = 30.0


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并设置一致的 PRAGMA。用完必须 close（或用 with）。"""
    conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # WAL：多读单写，读不阻塞写。这是我们并发模型的基础
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL：WAL 下的推荐档，掉电最多丢最后一个事务，性能与安全的平衡点
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path) -> None:
    """建库建表。幂等 —— 跑多少次都一样。

    父目录不存在会自动创建（首次启动 data/ 目录可能还没有）。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    schema = _SCHEMA.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)   # 全是 CREATE ... IF NOT EXISTS，可重复执行
        conn.commit()


def _now_ms() -> int:
    return int(time.time() * 1000)


# ══════════════════════════════════════════════════════════════════
# 路由状态
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RouteState:
    """一条路由的当前状态快照（只读视图）。"""

    route_id: str
    provider: str
    account: str | None
    model: str
    success_count: int
    fail_count: int
    consecutive_fail: int
    lease_owner: str | None
    lease_until: int
    cooldown_until: int
    quota_state: str
    quota_until: int


class RouteStateRepo:
    """路由健康状态读写。

    关键语义（整个路由层最容易写错的地方）：
        record_failure(is_health_failure=False)  → 配额耗尽，只影响该账号
        record_failure(is_health_failure=True)   → 服务故障，才进冷却
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def _ensure_row(self, conn: sqlite3.Connection, route_id: str) -> None:
        """确保行存在（INSERT OR IGNORE 比先查再插少一次往返，也避免竞态）。"""
        provider, _, rest = route_id.partition("/")
        model = rest or ""
        conn.execute(
            "INSERT OR IGNORE INTO route_state "
            "(route_id, provider, model, updated_at) VALUES (?, ?, ?, ?)",
            (route_id, provider, model, _now_ms()),
        )

    def get(self, route_id: str) -> RouteState | None:
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT * FROM route_state WHERE route_id = ?", (route_id,)
            ).fetchone()
        if row is None:
            return None
        return RouteState(
            route_id=row["route_id"],
            provider=row["provider"],
            account=row["account"],
            model=row["model"],
            success_count=row["success_count"],
            fail_count=row["fail_count"],
            consecutive_fail=row["consecutive_fail"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            cooldown_until=row["cooldown_until"],
            quota_state=row["quota_state"],
            quota_until=row["quota_until"],
        )

    def record_success(self, route_id: str) -> None:
        """记一次成功：成功数 +1，连续失败归零。"""
        now = _now_ms()
        with connect(self._db) as conn:
            self._ensure_row(conn, route_id)
            conn.execute(
                "UPDATE route_state SET success_count = success_count + 1, "
                "consecutive_fail = 0, last_ok_at = ?, updated_at = ? WHERE route_id = ?",
                (now, now, route_id),
            )
            conn.commit()

    def record_failure(self, route_id: str, *, is_health_failure: bool) -> None:
        """记一次失败。

        is_health_failure=False（配额耗尽）→ 只记数，不增加连续失败计数，
            因为这不是路由本身坏了，换号就好，不该把整条路由判死。
        is_health_failure=True（服务故障）→ 连续失败 +1，供冷却判定用。
        """
        now = _now_ms()
        with connect(self._db) as conn:
            self._ensure_row(conn, route_id)
            if is_health_failure:
                conn.execute(
                    "UPDATE route_state SET fail_count = fail_count + 1, "
                    "consecutive_fail = consecutive_fail + 1, last_fail_at = ?, "
                    "updated_at = ? WHERE route_id = ?",
                    (now, now, route_id),
                )
            else:
                conn.execute(
                    "UPDATE route_state SET fail_count = fail_count + 1, "
                    "last_fail_at = ?, updated_at = ? WHERE route_id = ?",
                    (now, now, route_id),
                )
            conn.commit()


# ══════════════════════════════════════════════════════════════════
# 破甲尝试
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TechniqueRate:
    """某「模型 × 技术」的历史成功率。"""

    technique: str
    rate: float
    n: int


class AttemptLogRepo:
    """破甲尝试明细。append-only，永不删除。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def record(
        self,
        *,
        request_id: str,
        model: str,
        level: int,
        technique: str,
        ok: bool,
        latency_ms: int = 0,
        refusal_kind: str | None = None,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute(
                "INSERT INTO attempt_log "
                "(request_id, model, level, technique, ok, refusal_kind, latency_ms, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, model, level, technique, 1 if ok else 0,
                 refusal_kind, latency_ms, _now_ms()),
            )
            conn.commit()

    def count(self) -> int:
        with connect(self._db) as conn:
            return conn.execute("SELECT COUNT(*) FROM attempt_log").fetchone()[0]

    def success_rates(
        self, *, model: str, min_samples: int = 5, since_ms: int | None = None
    ) -> list[TechniqueRate]:
        """按技术聚合成功率，只返回样本量达标的。

        min_samples 必须有：1/1 的 100% 没有统计意义。没有这个门槛，
        一个偶然成功的冷门技术会长期霸占首选位。

        注意：这只解决「有样本之后怎么排」，解决不了冷启动 ——
        25 技术 × 5 样本 = 125 次失败请求才建得起来。所以调用方必须先有
        内置先验（技术大类的默认顺序），攒够样本再覆盖先验。
        """
        sql = (
            "SELECT technique, SUM(ok) * 1.0 / COUNT(*) AS rate, COUNT(*) AS n "
            "FROM attempt_log WHERE model = ?"
        )
        params: list[object] = [model]
        if since_ms is not None:
            sql += " AND ts > ?"
            params.append(since_ms)
        sql += " GROUP BY technique HAVING n >= ? ORDER BY rate DESC, n DESC"
        params.append(min_samples)

        with connect(self._db) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [TechniqueRate(technique=r["technique"], rate=r["rate"], n=r["n"]) for r in rows]


# ══════════════════════════════════════════════════════════════════
# 请求日志
# ══════════════════════════════════════════════════════════════════


class RequestLogRepo:
    """一次请求一行。含路由四件套与 trace 快照（可回放）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def record(
        self,
        *,
        request_id: str,
        mode: str,
        ok: bool,
        route_requested: str | None = None,
        route_actual: str | None = None,
        route_source: str | None = None,
        model: str | None = None,
        jailbreak_level: int = 0,
        jailbreak_technique: str | None = None,
        jailbreak_attempts: int = 0,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        error_kind: str | None = None,
        latency_ms: int = 0,
        trace_json: str | None = None,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO request_log "
                "(request_id, mode, route_requested, route_actual, route_source, model, "
                " jailbreak_level, jailbreak_technique, jailbreak_attempts, "
                " prompt_tokens, completion_tokens, ok, error_kind, latency_ms, trace_json, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, mode, route_requested, route_actual, route_source, model,
                 jailbreak_level, jailbreak_technique, jailbreak_attempts,
                 prompt_tokens, completion_tokens, 1 if ok else 0, error_kind,
                 latency_ms, trace_json, _now_ms()),
            )
            conn.commit()

    def get(self, request_id: str) -> dict | None:
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT * FROM request_log WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None


# ══════════════════════════════════════════════════════════════════
# 成本账本
# ══════════════════════════════════════════════════════════════════


class BudgetLedgerRepo:
    """一次上游计费一行。窗口求和用于预算硬门判定。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db = str(db_path)

    def record(
        self,
        *,
        window_start: int,
        request_id: str,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with connect(self._db) as conn:
            conn.execute(
                "INSERT INTO budget_ledger "
                "(window_start, request_id, provider, model, input_tokens, output_tokens, cost_usd, ts) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (window_start, request_id, provider, model,
                 input_tokens, output_tokens, cost_usd, _now_ms()),
            )
            conn.commit()

    def window_total(self, *, window_start: int) -> float:
        """某窗口内的累计花费（美元）。

        不用内存计数器：按 window_start 查账本，重启不丢、多进程一致。
        """
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM budget_ledger WHERE window_start = ?",
                (window_start,),
            ).fetchone()
        return float(row[0])
