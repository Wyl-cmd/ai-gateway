"""G11 装配层 —— 把 config.toml 变成能跑的 GatewayDeps。

在这之前，`load_config()` 完整可用但只有测试在调：GUI 是个没接线的
显示壳，`gateway.demo` 用写死的假上游。这一层补上中间缺的一环：

    config.toml ──load_config()──▶ GatewayConfig ──assemble()──▶ GatewayDeps
                                                                     │
                                              create_app(deps) + uvicorn ◀┘

## 装配规则（每条都有对应的失败处理，绝不静默）

  provider 展开    provider × account × model → Candidate 列表
  适配器           provider × account 一个实例，键 (provider, key_id)
  key 解析         os.environ[account.key_env]，缺了 → 警告 + 跳过该账号
  models 为空      跳过整个 provider（没有模型清单就无法参与选路）
  auth_keys 空     不鉴权 —— 与 config.py「留空 = 仅本机安全」的语义一致

## 为什么适配器按 (provider, account) 键

同一 provider 的多把 key 必须各用各的适配器 —— API key 在适配器实例里
（build_headers / gemini 的 ?key= 查询串）。按 (provider, model) 键的话，
同型号的两把 key 只能留一把，账号轮巡就成了空话。
app._pick_adaptor 的查找顺序是 (provider, account) → (provider, model)
→ provider 兜底，所以 demo / 测试里旧的 (provider, model) 键仍然兼容。

## 权重映射的取舍

config 的 `w_cost` 在 RoutingPolicy 里没有对应项（评分器只有
health/quota/latency/fairness 四项），**显式忽略并在 docstring 里说清**，
而不是悄悄映射到某个近似项 —— 悄悄近似会让用户调权重时看到无法解释的行为。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping

from .app import GatewayDeps
from .config import GatewayConfig
from .jailbreak.artifact import ArtifactStore
from .jailbreak.memory import TechniqueMemory
from .observability.tokens import TokenMeter
from .providers.anthropic import AnthropicAdaptor
from .providers.base import Adaptor
from .providers.gemini import GeminiAdaptor
from .providers.openai import OpenAIAdaptor
from .routing.pool import Candidate, Pool, RoutingPolicy
from .storage.repo import AttemptLogRepo, init_db

#: kind → 适配器类。custom 走 OpenAI 兼容（市面上绝大多数自建网关都是）
KIND_TO_ADAPTOR: dict[str, type[Adaptor]] = {
    "openai": OpenAIAdaptor,
    "anthropic": AnthropicAdaptor,
    "gemini": GeminiAdaptor,
    "custom": OpenAIAdaptor,
}


def _warn(msg: str) -> None:
    """装配期警告。走 stderr —— stdout 可能被管道拿去做别的事。"""
    print(f"[gateway] {msg}", file=sys.stderr)


def build_pool(config: GatewayConfig) -> Pool:
    """从 RouteConfig 构建 Pool。两边默认值不同（config 更保守），以 config 为准。"""
    r = config.route
    return Pool(
        cooldown_threshold=r.cooldown_after_fails,
        cooldown_seconds=float(r.cooldown_seconds),
        lease_ttl=float(r.lease_seconds),
    )


def build_policy(config: GatewayConfig) -> RoutingPolicy:
    """RouteConfig 的 w_* 字段 → RoutingPolicy.weights 键名映射。

    - w_cost 被忽略（评分器没有 cost 项）
    - fairness 不在 config 里：**拿剩余预算，上限 0.15**。三项权重占满
      1.0 时 fairness=0 —— 预算怎么分以用户配置为准（测试抓过这个坑：
      固定补 0.15 会让默认配置的和变成 1.15，RoutingPolicy 直接拒绝）
    - 权重非法（和超 1 / 负值）不炸启动：警告 + 退回默认 RoutingPolicy
    """
    r = config.route
    h, q, l = r.w_health, r.w_quota, r.w_latency
    try:
        return RoutingPolicy(weights={
            "health": h,
            "quota": q,
            "latency": l,
            "fairness": max(0.0, min(0.15, 1.0 - (h + q + l))),
        })
    except ValueError as exc:
        _warn(f"评分权重非法（{exc}），退回默认 RoutingPolicy")
        return RoutingPolicy()


def load_secrets(path: str | os.PathLike[str] | None) -> dict[str, str]:
    """读密钥文件（secrets.toml，flat 的 变量名 = "密钥"）。

    不存在 = 空 dict（首次启动没有它很正常）。文件里只认字符串值。
    """
    if path is None:
        return {}
    import tomllib
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:                   # noqa: BLE001
        _warn(f"密钥文件 {p} 读取失败（当它不存在处理）: {exc}")
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def assemble(
    config: GatewayConfig,
    env: Mapping[str, str] | None = None,
    secrets_path: str | os.PathLike[str] | None = "secrets.toml",
) -> GatewayDeps:
    """装配主入口。env 参数便于测试注入，默认读 os.environ。

    密钥查找顺序：**环境变量 > secrets.toml**（显式环境变量是用户手动设的，
    应该赢；secrets.toml 是设置页写的常规存放处）。两个地方都没有 → 跳账号。

    不会因为个别账号/供应商配置不全而失败 —— 网关的哲学是「能跑多少跑多少」，
    缺的东西警告出来让用户知道，而不是整个启动失败。
    """
    # 环境变量优先于密钥文件（与 load_config「环境变量 > 文件」一致）
    e = {**load_secrets(secrets_path),
         **(os.environ if env is None else dict(env))}

    pool = build_pool(config)
    candidates: list[Candidate] = []
    adaptors: dict[tuple[str, str], Adaptor] = {}

    for p in config.providers:
        if not p.enabled:
            continue
        if not p.models:
            _warn(f"provider {p.name} 没配 models，跳过（无法参与选路）")
            continue

        cls = KIND_TO_ADAPTOR.get(p.kind)
        if cls is None:                        # 理论上 pydantic 已挡住，双保险
            _warn(f"provider {p.name} 的 kind={p.kind} 没有适配器，跳过")
            continue

        for acc in p.accounts:
            key = e.get(acc.key_env, "")
            if not key:
                _warn(f"provider {p.name} 账号 {acc.key_id} 的环境变量 "
                      f"{acc.key_env} 未设置，跳过该账号")
                continue
            # 每账号一个实例：API key 活在适配器里（见模块 docstring）
            adaptors[(p.name, acc.key_id)] = cls(p.base_url, key)
            for m in p.models:
                candidates.append(
                    Candidate(provider=p.name, account=acc.key_id, model=m)
                )

    if not candidates:
        _warn("没有任何可用候选 —— 检查 config.toml 的 providers/models/环境变量")

    # auth_keys 空 = 不鉴权（config.py 的既定语义）。绑定非本机地址时提醒一句。
    if not config.auth_keys and config.server.host not in ("127.0.0.1", "localhost"):
        _warn("auth_keys 为空 = 网关不鉴权，而监听地址不是本机回环 —— "
              "局域网内任何人都能用你的上游 key")

    # 破甲记忆/产物库：SQLite 就绪就接上（能学就学，失败不挡路）
    memory: Any = None
    artifacts: Any = None
    try:
        from pathlib import Path
        Path(config.storage.db_path).parent.mkdir(parents=True, exist_ok=True)
        init_db(config.storage.db_path)
        memory = TechniqueMemory(repo=AttemptLogRepo(config.storage.db_path))
        artifacts = ArtifactStore(config.storage.db_path)
    except Exception as exc:                   # noqa: BLE001
        _warn(f"记忆/产物库初始化失败（不影响启动，只是学不到东西）: {exc}")

    return GatewayDeps(
        pool=pool,
        candidates=candidates,
        adaptors=adaptors,
        meter=TokenMeter(),
        api_key=config.auth_keys[0] if config.auth_keys else "",
        api_keys=list(config.auth_keys),
        jailbreak_enabled=config.jailbreak.enabled,
        memory=memory,
        artifacts=artifacts,
        mode=config.mode.default,
        policy=config.mode.fallback,
        known_providers={p.name for p in config.providers},
        routing_policy=build_policy(config),
    )


def lan_ips() -> list[str]:
    """列出本机的局域网 IP —— 启动横幅用，方便把地址直接抄给手机/平板。"""
    import socket
    ips: list[str] = []
    try:
        # 连一个外网地址（不发包）问内核用了哪个出口 IP —— 最可靠的取法
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("223.5.5.5", 53))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips
