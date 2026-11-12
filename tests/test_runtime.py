"""G11 装配层测试 —— config.toml → GatewayDeps 的展开规则。

每条装配规则都必须有失败处理（跳过 + 警告），绝不能静默：
这里测的就是「配置不完整时，哪些进了候选池、哪些没进」。
"""

from __future__ import annotations

from gateway.config import (
    GatewayConfig,
    ProviderAccount,
    ProviderConfig,
    ServerConfig,
    StorageConfig,
)
from gateway.routing.pool import RoutingPolicy
from gateway.runtime import assemble, build_policy

import pytest


def _provider(name="fake", models=("m1",), accounts=("1", "2"), env_suffix="X"):
    """速构一个 provider 配置。accounts 里每个元素同时生成 key 环境变量名。"""
    return ProviderConfig(
        name=name,
        kind="openai",
        base_url="http://up.test",
        models=list(models),
        accounts=[
            ProviderAccount(key_id=f"key{a}", key_env=f"FAKE_{env_suffix}_KEY_{a}")
            for a in accounts
        ],
    )


def _config(providers, auth_keys=("gw-key",), tmp_path=None):
    return GatewayConfig(
        server=ServerConfig(host="127.0.0.1"),
        storage=StorageConfig(db_path=str(tmp_path / "t.db") if tmp_path else
                              "./data/never-test.db"),
        providers=list(providers),
        auth_keys=list(auth_keys),
    )


# ═══════════════════════════════════════════════════════════════
# 展开
# ═══════════════════════════════════════════════════════════════
def test_按账号乘模型展开候选(tmp_path):
    cfg = _config([_provider(models=("m1", "m2"), accounts=("1", "2"))],
                  tmp_path=tmp_path)
    deps = assemble(cfg, env={})
    # 1 provider × 2 账号 × 2 模型 = 4 候选（env 为空 → key 全缺，全被跳过）
    assert deps.candidates == []


def test_key_齐全时_候选与适配器都展开(tmp_path):
    p = _provider(models=("m1", "m2"), accounts=("1", "2"))
    cfg = _config([p], tmp_path=tmp_path)
    env = {"FAKE_X_KEY_1": "sk-1", "FAKE_X_KEY_2": "sk-2"}
    deps = assemble(cfg, env=env)

    assert len(deps.candidates) == 4
    # 适配器键是 (provider, key_id) —— 一账号一实例，多把 key 各用各的
    assert set(deps.adaptors) == {("fake", "key1"), ("fake", "key2")}
    assert deps.adaptors[("fake", "key1")].api_key == "sk-1"
    assert deps.adaptors[("fake", "key2")].api_key == "sk-2"
    # 候选覆盖 账号 × 模型 全组合
    keys = {c.key for c in deps.candidates}
    assert keys == {("fake", "key1", "m1"), ("fake", "key1", "m2"),
                    ("fake", "key2", "m1"), ("fake", "key2", "m2")}


def test_缺_key_env_只跳过该账号(tmp_path):
    p = _provider(accounts=("1", "2"))
    cfg = _config([p], tmp_path=tmp_path)
    deps = assemble(cfg, env={"FAKE_X_KEY_1": "sk-1"})   # key2 的环境变量没设
    assert {c.account for c in deps.candidates} == {"key1"}
    assert set(deps.adaptors) == {("fake", "key1")}


def test_models_为空_跳过整个_provider(tmp_path):
    cfg = _config([_provider(models=()), _provider(name="ok")],
                  tmp_path=tmp_path)
    env = {"FAKE_X_KEY_1": "sk-1"}
    deps = assemble(cfg, env=env)
    assert {c.provider for c in deps.candidates} == {"ok"}


def test_enabled_false_跳过(tmp_path):
    p = _provider()
    p = p.model_copy(update={"enabled": False})
    deps = assemble(_config([p], tmp_path=tmp_path), env={"FAKE_X_KEY_1": "sk-1"})
    assert deps.candidates == []


# ═══════════════════════════════════════════════════════════════
# 鉴权与模式
# ═══════════════════════════════════════════════════════════════
def test_auth_keys_传成列表(tmp_path):
    deps = assemble(_config([_provider()], auth_keys=("k1", "k2")),
                    env={"FAKE_X_KEY_1": "sk-1"}, )
    assert deps.api_keys == ["k1", "k2"]
    assert deps.api_key == "k1"


def test_auth_keys_空_不鉴权(tmp_path):
    deps = assemble(_config([_provider()], auth_keys=()),
                    env={"FAKE_X_KEY_1": "sk-1"})
    assert deps.api_keys == []
    assert deps.api_key == ""


def test_模式与降级策略来自配置(tmp_path):
    cfg = _config([_provider()], tmp_path=tmp_path)
    cfg = cfg.model_copy(update={"mode": cfg.mode.model_copy(
        update={"default": "manual", "fallback": "fallback_same_model"})})
    deps = assemble(cfg, env={"FAKE_X_KEY_1": "sk-1"})
    assert deps.mode == "manual"
    assert deps.policy == "fallback_same_model"
    assert deps.known_providers == {"fake"}


# ═══════════════════════════════════════════════════════════════
# 权重映射
# ═══════════════════════════════════════════════════════════════
def test_权重映射_health_quota_latency(tmp_path):
    cfg = _config([], tmp_path=tmp_path)
    cfg = cfg.model_copy(update={"route": cfg.route.model_copy(
        update={"w_health": 0.6, "w_quota": 0.2, "w_latency": 0.1})})
    pol = build_policy(cfg)
    assert pol.w("health") == 0.6
    assert pol.w("quota") == 0.2
    assert pol.w("latency") == 0.1
    # fairness = 剩余预算（1 - 0.9 = 0.1），上限 0.15
    assert pol.w("fairness") == pytest.approx(0.1)


def test_默认权重的_fairness_归零(tmp_path):
    """config 默认三项和恰为 1.0 —— fairness 拿不到预算，必须是 0。

    固定补 0.15 的做法会让总和 1.15，RoutingPolicy 直接 ValueError
    （这个坑就是 TDD 红灯抓出来的）。
    """
    pol = build_policy(_config([], tmp_path=tmp_path))   # 全默认值
    assert pol.w("health") == 0.5
    assert pol.w("quota") == 0.3
    assert pol.w("latency") == 0.2
    assert pol.w("fairness") == 0.0


def test_权重非法_退回默认策略(tmp_path):
    cfg = _config([], tmp_path=tmp_path)
    cfg = cfg.model_copy(update={"route": cfg.route.model_copy(
        update={"w_health": 0.9, "w_quota": 0.9})})       # 和超 1
    pol = build_policy(cfg)
    assert pol.weights == dict(RoutingPolicy().weights)   # 原样默认


# ═══════════════════════════════════════════════════════════════
# 记忆/产物库
# ═══════════════════════════════════════════════════════════════
def test_sqlite_就绪时_接上记忆和产物库(tmp_path):
    deps = assemble(_config([_provider()], tmp_path=tmp_path),
                    env={"FAKE_X_KEY_1": "sk-1"})
    assert deps.memory is not None
    assert deps.artifacts is not None
