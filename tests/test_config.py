"""G0 门②：配置加载的测试。

四条断言，覆盖配置加载的全部行为：
    ① 无配置文件 → 全部用默认值
    ② TOML 能覆盖默认值
    ③ 环境变量能覆盖 TOML
    ④ 非法取值 → 抛 ValidationError（不是静默接受）

**这四条在实现之前必定红** —— 因为 `gateway.config` 只有空壳，函数体是
`raise NotImplementedError`。红因是 `NotImplementedError`（功能没实现），
不是 `ModuleNotFoundError`（文件不存在）—— 后者说明骨架都没建好，不算走门②。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_无配置文件时用默认值(tmp_path):
    """不提供 config.toml，全部字段用默认值。"""
    from gateway.config import load_config

    cfg = load_config(config_path=None)

    assert cfg.server.host == "0.0.0.0"          # 默认只监听本地
    assert cfg.server.port == 8787
    assert cfg.mode.default == "auto"
    assert cfg.jailbreak.enabled is True           # 破甲默认开
    assert cfg.jailbreak.start_level == 1          # 从 L1 起（L0 总是先加）
    assert cfg.jailbreak.max_level == 2            # L3/L4 默认不碰
    assert cfg.budget.enabled is False             # 预算默认关
    assert cfg.route.w_cost == 0.0                 # 不偷偷省钱


def test_TOML覆盖默认值(tmp_path):
    """config.toml 里的值覆盖默认值。"""
    from gateway.config import load_config

    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        """
[server]
host = "0.0.0.0"
port = 9999

[mode]
default = "manual"

[jailbreak]
enabled = false
max_level = 4
""",
        encoding="utf-8",
    )

    cfg = load_config(config_path=toml_file)

    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9999
    assert cfg.mode.default == "manual"
    assert cfg.jailbreak.enabled is False
    assert cfg.jailbreak.max_level == 4
    # 没在 TOML 里写的字段仍是默认值
    assert cfg.jailbreak.start_level == 1


def test_环境变量覆盖TOML(tmp_path, monkeypatch):
    """环境变量优先级最高，能覆盖 TOML 里的值。"""
    from gateway.config import load_config

    toml_file = tmp_path / "config.toml"
    toml_file.write_text(
        """
[server]
port = 9999

[mode]
default = "manual"
""",
        encoding="utf-8",
    )

    # 双下划线表示嵌套：GATEWAY_SERVER__PORT → server.port
    monkeypatch.setenv("GATEWAY_SERVER__PORT", "1234")
    monkeypatch.setenv("GATEWAY_MODE__DEFAULT", "auto")

    cfg = load_config(config_path=toml_file)

    assert cfg.server.port == 1234          # 环境变量赢了 TOML
    assert cfg.mode.default == "auto"       # 环境变量赢了 TOML
    assert cfg.server.host == "0.0.0.0"   # 默认监听所有网卡（局域网可访问）


def test_非法取值抛ValidationError(tmp_path):
    """非法取值必须抛错，不能静默接受或退回默认值。

    静默退回默认值是配置系统最危险的失败模式 —— 用户以为配了 A，
    实际跑的是默认的 B，而且没有任何提示。
    """
    from gateway.config import load_config

    # ① 枚举字段给了不认识的值
    bad_mode = tmp_path / "bad_mode.toml"
    bad_mode.write_text('[mode]\ndefault = "全自动"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(config_path=bad_mode)

    # ② 端口超出范围
    bad_port = tmp_path / "bad_port.toml"
    bad_port.write_text("[server]\nport = 99999\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(config_path=bad_port)

    # ③ 类型不对（字符串给了数字字段）
    bad_type = tmp_path / "bad_type.toml"
    bad_type.write_text('[server]\nport = "不是数字"\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(config_path=bad_type)


# ═══════════════════════════════════════════════════════════════
# 局域网访问
# ═══════════════════════════════════════════════════════════════
def test_默认监听所有网卡方便局域网访问():
    """默认 0.0.0.0 而不是 127.0.0.1 —— 手机/平板要能连。

    安全边界靠 api_key 而不是绑定地址：绑回环只能防「同网段误连」，
    却让局域网设备全用不了。真正的防护是 key + 局域网可信假设。
    """
    from gateway.config import load_config

    # 空配置 → 用默认值
    cfg = load_config()
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 8787


def test_host_可以被配置文件覆盖成回环():
    """想只本机用的人也要能改回去。"""
    from gateway.config import load_config
    import tempfile, pathlib as _pl

    with tempfile.TemporaryDirectory() as d:
        p = _pl.Path(d) / "c.toml"
        p.write_text('[server]\nhost = "127.0.0.1"\n', encoding="utf-8")
        cfg = load_config(str(p))
    # 配置文件明确写了回环 → 尊重它（这条不能被默认值的改动影响）
    assert cfg.server.host == "127.0.0.1"
