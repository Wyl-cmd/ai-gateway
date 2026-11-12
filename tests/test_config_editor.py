"""设置页的纯逻辑层测试 —— GatewayConfig ⇄ TOML 文本 序列化。

GUI 对话框保存时调用 serialize_config 写回 config.toml。
核心保证：**写出去的文件 load_config 读回来，值必须一致**（往返无损），
否则「设置页保存」就会悄悄丢配置。
"""

from __future__ import annotations

import sys

from gateway.config import (
    GatewayConfig,
    ProviderAccount,
    ProviderConfig,
    load_config,
)


def _two_provider_cfg():
    return GatewayConfig.model_validate({
        "auth_keys": ["gw-a", "gw-b"],
        "server": {"host": "127.0.0.1", "port": 9001},
        "storage": {"db_path": "data/x.db"},
        "mode": {"default": "manual", "fallback": "fallback_same_model"},
        "jailbreak": {"enabled": False, "max_level": 3},
        "providers": [
            {"name": "deepseek", "kind": "openai",
             "base_url": "https://api.deepseek.com",
             "models": ["deepseek-chat", "deepseek-reasoner"],
             "accounts": [
                 {"key_id": "key1", "key_env": "DS_KEY_1", "quota": "paid"},
                 {"key_id": "key2", "key_env": "DS_KEY_2"},
             ]},
            {"name": "我家中转", "kind": "custom",
             "base_url": "http://192.168.1.5:3000/v1",
             "models": ["gpt-4o"],
             "accounts": [{"key_id": "only", "key_env": "MY_KEY"}]},
        ],
    })


# ═══════════════════════════════════════════════════════════════
# 往返无损
# ═══════════════════════════════════════════════════════════════
def test_序列化后读回_配置等价(tmp_path):
    from gateway.gui.config_editor import serialize_config

    cfg = _two_provider_cfg()
    path = tmp_path / "config.toml"
    path.write_text(serialize_config(cfg), encoding="utf-8")

    back = load_config(path, env={})
    assert back == cfg, "往返后配置变了 —— 设置页保存会丢配置"


def test_中文与特殊字符_往返无损(tmp_path):
    from gateway.gui.config_editor import serialize_config

    cfg = GatewayConfig.model_validate({
        "providers": [
            {"name": '带"引号"的名字', "kind": "openai",
             "base_url": "http://x",
             "models": ["模型一"],
             "accounts": [{"key_id": "k\"1", "key_env": "K1"}]},
        ],
    })
    path = tmp_path / "config.toml"
    path.write_text(serialize_config(cfg), encoding="utf-8")
    back = load_config(path, env={})
    assert back.providers[0].name == '带"引号"的名字'
    assert back.providers[0].accounts[0].key_id == "k\"1"
    assert back.providers[0].models == ["模型一"]


def test_空配置_序列化_读回仍是空(tmp_path):
    from gateway.gui.config_editor import serialize_config

    cfg = GatewayConfig()
    path = tmp_path / "config.toml"
    path.write_text(serialize_config(cfg), encoding="utf-8")
    back = load_config(path, env={})
    assert back.providers == []
    assert back.auth_keys == []


# ═══════════════════════════════════════════════════════════════
# 密钥写系统环境变量（setx）
# ═══════════════════════════════════════════════════════════════
def test_windows_下_密钥写setx并更新当前进程(monkeypatch):
    from gateway.gui import config_editor

    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(config_editor.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setenv("EDITOR_TEST_KEY", "")

    config_editor.set_user_env_var("EDITOR_TEST_KEY", "sk-secret")

    assert calls and list(calls[0][0][:3]) == ["setx", "EDITOR_TEST_KEY",
                                               "sk-secret"]
    import os
    assert os.environ["EDITOR_TEST_KEY"] == "sk-secret"   # 当前进程立即生效


def test_非windows_只更新当前进程(monkeypatch):
    from gateway.gui import config_editor

    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config_editor.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setenv("EDITOR_TEST_KEY2", "")

    config_editor.set_user_env_var("EDITOR_TEST_KEY2", "sk-x")

    assert calls == [], "非 Windows 不该调 setx"
    import os
    assert os.environ["EDITOR_TEST_KEY2"] == "sk-x"


# ═══════════════════════════════════════════════════════════════
# 环境变量名自动生成（用户不该手动填这个）
# ═══════════════════════════════════════════════════════════════
def test_auto_key_env_按供应商名生成():
    from gateway.gui.config_editor import auto_key_env

    assert auto_key_env("deepseek", 1) == "DEEPSEEK_KEY_1"
    assert auto_key_env("deepseek", 2) == "DEEPSEEK_KEY_2"


def test_auto_key_env_中文名和特殊字符():
    """中文名 → 拼音没法自动做，退而求其次：非 ASCII 字母数字全转下划线，
    纯中文名退化成空前缀（KEY_1）。保证是合法环境变量名、setx 能接受。"""
    from gateway.gui.config_editor import auto_key_env

    assert auto_key_env("我家中转", 1) == "KEY_1"
    assert auto_key_env("my-relay 2", 3) == "MY_RELAY_2_KEY_3"
