"""设置页的纯逻辑层测试 —— GatewayConfig ⇄ TOML 文本 序列化。

GUI 对话框保存时调用 serialize_config 写回 config.toml。
核心保证：**写出去的文件 load_config 读回来，值必须一致**（往返无损），
否则「设置页保存」就会悄悄丢配置。
"""

from __future__ import annotations

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
# 密钥存储键自动生成（用户不该手动填这个）
#
# 历史注记：密钥曾走过「写系统环境变量（setx）」的路线，后改为本地
# secrets.toml 文件 —— 平台无关、不用改系统设置、对新旧进程都生效。
# 相关的 set_user_env_var 函数及其测试已随之删除。
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


# ═══════════════════════════════════════════════════════════════
# 密钥文件写入（secrets.toml，合并语义）
# ═══════════════════════════════════════════════════════════════
def test_save_secrets_保留文件里已有的其他密钥(tmp_path):
    """只提交一家的密钥时，文件里另一家的必须原样保留 ——
    否则设置页改一家会把别家的 key 弄丢。"""
    from gateway.gui.config_editor import save_secrets

    p = tmp_path / "secrets.toml"
    p.write_text('OTHER_KEY = "sk-old"\n', encoding="utf-8")

    save_secrets(p, {"MY_KEY": "sk-new"})

    text = p.read_text(encoding="utf-8")
    assert "OTHER_KEY" in text and "sk-old" in text
    assert "MY_KEY" in text and "sk-new" in text


def test_save_secrets_覆盖同名密钥(tmp_path):
    from gateway.gui.config_editor import save_secrets

    p = tmp_path / "secrets.toml"
    p.write_text('MY_KEY = "sk-old"\n', encoding="utf-8")
    save_secrets(p, {"MY_KEY": "sk-rotated"})
    assert 'MY_KEY = "sk-rotated"' in p.read_text(encoding="utf-8")
    assert "sk-old" not in p.read_text(encoding="utf-8")


def test_save_secrets_文件不存在则创建_含转义(tmp_path):
    """密钥里的引号/反斜杠/Unicode 要安全落盘且能被 tomllib 读回。"""
    import tomllib

    from gateway.gui.config_editor import save_secrets

    p = tmp_path / "secrets.toml"
    save_secrets(p, {'K1': 'sk-"x"\y', "K2": "密钥中文"})
    back = tomllib.loads(p.read_text(encoding="utf-8"))
    assert back == {"K1": 'sk-"x"\y', "K2": "密钥中文"}


def test_save_secrets_空提交不动文件(tmp_path):
    """没填任何新密钥（全留空）→ 文件保持原样（不改 mtime 内容）。"""
    from gateway.gui.config_editor import save_secrets

    p = tmp_path / "secrets.toml"
    original = 'A = "1"\n'
    p.write_text(original, encoding="utf-8")
    save_secrets(p, {})
    assert p.read_text(encoding="utf-8") == original
