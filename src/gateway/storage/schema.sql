-- AI 网关数据表定义（SQLite，WAL 模式）
-- 全部用 IF NOT EXISTS：init_db 可重复执行（幂等）

-- ─────────────────────────────────────────────────────────────
-- 路由健康状态：租约 + 健康 + 冷却 + 配额合一
--
-- 为什么合成一张表：拆成三张会在并发时读到不一致状态
-- （租约在 A 表已释放、健康在 B 表还是旧值）。一张表 + 一条事务 = 原子。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS route_state (
    route_id         TEXT PRIMARY KEY,       -- "openai-official/gpt-4o"
    provider         TEXT NOT NULL DEFAULT '',
    account          TEXT,                   -- 只存 key_id，不存 key 本体
    model            TEXT NOT NULL DEFAULT '',

    success_count    INTEGER NOT NULL DEFAULT 0,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_fail INTEGER NOT NULL DEFAULT 0,

    lease_owner      TEXT,                   -- 持有租约的请求 id
    lease_until      INTEGER NOT NULL DEFAULT 0,   -- epoch 毫秒

    cooldown_until   INTEGER NOT NULL DEFAULT 0,

    quota_state      TEXT NOT NULL DEFAULT 'ok',   -- ok | exhausted
    quota_until      INTEGER NOT NULL DEFAULT 0,

    last_ok_at       INTEGER,
    last_fail_at     INTEGER,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_route_state_cooldown ON route_state(cooldown_until);

-- ─────────────────────────────────────────────────────────────
-- 破甲尝试明细：append-only，永不删除
-- 这是「模型 × 技术 成功率矩阵」的唯一数据源
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS attempt_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    model        TEXT NOT NULL,
    level        INTEGER NOT NULL,       -- 0~4
    technique    TEXT NOT NULL,          -- "identity" / "rot13" / "deepinception"
    ok           INTEGER NOT NULL,       -- 1 成功 / 0 被拒
    refusal_kind TEXT,                   -- phrase | structure | model | judge | partial
    latency_ms   INTEGER NOT NULL DEFAULT 0,
    ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempt_model_tech ON attempt_log(model, technique);

-- ─────────────────────────────────────────────────────────────
-- 请求日志：一次请求一行（含路由决策快照，可回放）
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS request_log (
    request_id        TEXT PRIMARY KEY,
    mode              TEXT NOT NULL,          -- auto | manual
    route_requested   TEXT,                   -- 请求时指定的（auto 时为 NULL）
    route_actual      TEXT,                   -- 实际用的
    route_source      TEXT,                   -- auto_rank | manual_lock | ...
    model             TEXT,

    jailbreak_level   INTEGER NOT NULL DEFAULT 0,
    jailbreak_technique TEXT,
    jailbreak_attempts INTEGER NOT NULL DEFAULT 0,

    prompt_tokens     INTEGER,
    completion_tokens INTEGER,

    ok                INTEGER NOT NULL,
    error_kind        TEXT,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    trace_json        TEXT,                   -- RouteTrace JSON，可回放
    ts                INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);

-- ─────────────────────────────────────────────────────────────
-- 成本账本：一次上游计费一行
--
-- 为什么和 request_log 分开：破甲重试会调多次上游，
-- 一次请求产生多条计费记录。混一张表就没法按窗口求和。
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS budget_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start  INTEGER NOT NULL,       -- 窗口起点（按 hourly/daily/monthly 取整）
    request_id    TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    ts            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_budget_window ON budget_ledger(window_start);
