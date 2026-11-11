"""配置加载：TOML 文件 + 环境变量覆盖。

**优先级**：环境变量 > TOML 文件 > 默认值

设计取舍（为什么不用 pydantic-settings 的 TOML 源）：
    pydantic-settings 的 `TomlConfigSettingsSource` 在 `settings_customise_sources`
    里写死，TOML 路径要在类定义时确定 —— 而我们需要「测试里传临时路径、运行时传工作目录」。
    手动四步（读 TOML → 铺默认 → 覆盖环境变量 → 校验）比扩展它的源链更透明，
    出错时也能一眼看出是哪一步的问题。

术语对照：
    TOML（Tom's Obvious Minimal Language，一种配置文件格式）
    环境变量（environment variable）
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "GatewayConfig",
    "ServerConfig",
    "StorageConfig",
    "ModeConfig",
    "RouteConfig",
    "JailbreakConfig",
    "BudgetConfig",
    "ProviderConfig",
    "load_config",
]


# ══════════════════════════════════════════════════════════════════
# 各配置段
# ══════════════════════════════════════════════════════════════════


class ServerConfig(BaseModel):
    """HTTP 服务监听设置。"""

    # 默认只监听回环地址 —— 与 C2 那次教训一致：要对外必须显式改
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)


class StorageConfig(BaseModel):
    """SQLite 存储路径。"""

    db_path: str = "./data/gateway.db"


class ModeConfig(BaseModel):
    """模式层默认值。"""

    default: Literal["auto", "manual"] = "auto"


class RouteConfig(BaseModel):
    """路由排序权重与健康管理参数。

    w_cost 默认 0.0：不偷偷省钱。开了 --prefer-cheap 才参与排序。
    """

    w_health: float = Field(default=0.5, ge=0.0, le=1.0)
    w_quota: float = Field(default=0.3, ge=0.0, le=1.0)
    w_latency: float = Field(default=0.2, ge=0.0, le=1.0)
    w_cost: float = Field(default=0.0, ge=0.0, le=1.0)

    cooldown_after_fails: int = Field(default=3, ge=1)
    cooldown_seconds: int = Field(default=300, ge=1)
    lease_seconds: int = Field(default=30, ge=1)


class JailbreakConfig(BaseModel):
    """破甲能力设置。

    start_level=1：L0（身份锚定）总是先加，所以从 L1 起算。
    max_level=2：L3 要花钱、L4 很慢，默认不碰。
    """

    enabled: bool = True
    start_level: int = Field(default=1, ge=0, le=4)
    max_level: int = Field(default=2, ge=0, le=4)
    ladder_timeout_ms: int = Field(default=12000, ge=1000)


class BudgetConfig(BaseModel):
    """成本预算硬门。默认关闭 —— 不开则只记账不拦。"""

    enabled: bool = False
    window: Literal["hourly", "daily", "monthly"] = "daily"
    limit_usd: float = Field(default=1.0, gt=0)
    on_exceed: Literal["warn", "downgrade", "reject"] = "downgrade"
    downgrade_to: list[str] = Field(default_factory=list)


class ProviderAccount(BaseModel):
    """上游账号。

    只存 key_id 标识，真 key 从环境变量读（key_env 指定变量名）。
    这样配置文件可以安全备份/分享，数据库泄露也不等于密钥泄露。
    """

    key_id: str
    key_env: str
    quota: str = "unlimited"


class ProviderPricing(BaseModel):
    """某模型的计价（每 1K token 的美元价）。"""

    model: str
    input_per_1k: float = Field(ge=0.0)
    output_per_1k: float = Field(ge=0.0)


class ProviderConfig(BaseModel):
    """一个上游供应商。"""

    name: str
    # kind 决定用哪个协议适配器
    kind: Literal["openai", "anthropic", "gemini", "custom"] = "openai"
    base_url: str
    enabled: bool = True
    accounts: list[ProviderAccount] = Field(default_factory=list)
    pricing: list[ProviderPricing] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# 聚合根
# ══════════════════════════════════════════════════════════════════


class GatewayConfig(BaseModel):
    """网关完整配置。所有段都可缺省，缺省即用默认值。"""

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    mode: ModeConfig = Field(default_factory=ModeConfig)
    route: RouteConfig = Field(default_factory=RouteConfig)
    jailbreak: JailbreakConfig = Field(default_factory=JailbreakConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)

    # 客户端鉴权用的网关令牌（不是上游 key）。留空 = 不鉴权（仅 127.0.0.1 安全）
    auth_keys: list[str] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# 加载
# ══════════════════════════════════════════════════════════════════

ENV_PREFIX = "GATEWAY_"
ENV_NESTED_DELIMITER = "__"


def _set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    """把 value 写进 target 的嵌套路径，路径不存在则逐层建。

    例：path=["server","port"], value="1234" → target["server"]["port"] = "1234"
    """
    node = target
    for key in path[:-1]:
        existing = node.get(key)
        if not isinstance(existing, dict):
            # 该位置原本是标量或不存在 → 用字典覆盖（环境变量优先级更高）
            existing = {}
            node[key] = existing
        node = existing
    node[path[-1]] = value


def _collect_env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """从环境变量里挑出 GATEWAY_ 开头的，转成嵌套字典。

    GATEWAY_SERVER__PORT=1234        → {"server": {"port": "1234"}}
    GATEWAY_MODE__DEFAULT=auto       → {"mode": {"default": "auto"}}
    GATEWAY_AUTH_KEYS=["a","b"]      → {"auth_keys": "[\"a\",\"b\"]"}  ← pydantic 负责解析 JSON

    值一律保持字符串，交给 pydantic 做类型转换 —— 这样类型不匹配会抛
    ValidationError（而不是我们手写的转换悄悄吞掉）。
    """
    overrides: dict[str, Any] = {}
    for raw_key, raw_val in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue

        # 去掉前缀，转小写，按双下划线切分
        remainder = raw_key[len(ENV_PREFIX) :]
        path = [seg.lower() for seg in remainder.split(ENV_NESTED_DELIMITER) if seg]
        if not path:
            continue

        _set_nested(overrides, path, raw_val)
    return overrides


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """递归合并 overlay 到 base 上，overlay 优先。返回新字典，不改入参。"""
    result = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(
    config_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> GatewayConfig:
    """加载并校验配置。

    四步，顺序即优先级：
        ① 读 TOML（不存在则跳过，不报错 —— 支持零配置启动）
        ② 收集环境变量覆盖
        ③ 合并（环境变量赢）
        ④ pydantic 校验（非法值抛 ValidationError）

    Args:
        config_path: TOML 路径。None 或文件不存在 → 只用默认值 + 环境变量。
        env: 环境变量字典，默认取 os.environ。测试时注入用。

    Raises:
        pydantic.ValidationError: 任一字段类型/范围/枚举不合法。
    """
    # ① 读 TOML
    file_data: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path)
        if path.is_file():
            with path.open("rb") as fh:
                file_data = tomllib.load(fh)
        # 文件不存在不报错：允许「零配置启动」，全部走默认值

    # ② 环境变量
    env_data = _collect_env_overrides(dict(env if env is not None else os.environ))

    # ③ 合并：环境变量优先于文件
    merged = _deep_merge(file_data, env_data)

    # ④ 校验
    return GatewayConfig.model_validate(merged)
