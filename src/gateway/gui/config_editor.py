"""设置页的纯逻辑层 —— GatewayConfig ⇄ TOML 文本 + 密钥文件读写。

## 为什么手写序列化而不用 toml 库

Python 标准库只有读（tomllib），没有写；引第三方 toml 写库只为了
一个固定结构的文件不值当（项目的「少一个依赖少一个炸点」哲学，
参考 cli.py 不用 rich 的决策）。结构是我们自己定的、字段有限，
手写生成最透明 —— 出问题一眼能看出是哪行。

字符串转义用 json.dumps：它产出的转义集（\\" \\\\ \\uXXXX）
是 TOML 基本字符串（basic string）的合法子集，中文原样保留。

## 密钥的安全边界

密钥值**永远不进 config.toml**（文件可能被分享/进仓库/被同步盘泄露），
而是放在同目录的 **secrets.toml**（已被 .gitignore 忽略）。

不用环境变量的理由：写入是平台相关的（Windows setx / Linux export），
且只对新进程生效；本地文件 Windows/Linux 通吃，网关在哪跑都一样。
读取端仍兼容环境变量（runtime.assemble：环境变量 > secrets.toml）——
已经用环境变量部署过的人不受影响。
"""

from __future__ import annotations

import json
import os

from ..config import GatewayConfig

__all__ = ["serialize_config", "save_secrets", "auto_key_env"]


def auto_key_env(provider_name: str, index: int) -> str:
    """按供应商名自动生成密钥存储键：deepseek → DEEPSEEK_KEY_1。

    这个键是 secrets.toml 里找密钥用的名字（历史字段名叫 key_env ——
    环境变量时代的遗产，语义现在就是「密钥的存储键」）。
    用户不该手动填 —— 这是实现细节。规则：非 ASCII 字母数字一律转
    下划线，中文名会退化成空前缀（KEY_1），保证是合法 TOML 键。
    """
    prefix = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_"
        for ch in provider_name.upper()
    ).strip("_")
    return f"{prefix}_KEY_{index}" if prefix else f"KEY_{index}"


def _s(value: str) -> str:
    """字符串 → TOML 基本字符串字面量。"""
    return json.dumps(value, ensure_ascii=False)


def _arr(values: list[str]) -> str:
    return "[" + ", ".join(_s(v) for v in values) + "]"


def serialize_config(cfg: GatewayConfig) -> str:
    """GatewayConfig → config.toml 文本。

    写全所有段（含默认值）—— 设置页保存后文件就是完整自描述的，
    用户手改任何一项都有据可依。往返无损由 tests/test_config_editor.py 保证。
    """
    lines: list[str] = []

    # auth_keys 是顶层键，TOML 语法要求必须写在任何 [表头] 之前
    if cfg.auth_keys:
        lines.append("auth_keys = " + _arr(list(cfg.auth_keys)))

    lines += [
        "",
        "[server]",
        f"host = {_s(cfg.server.host)}",
        f"port = {cfg.server.port}",
        "",
        "[storage]",
        f"db_path = {_s(cfg.storage.db_path)}",
        "",
        "[mode]",
        "# 注意字段名是 default（不是 mode）—— 写错会被静默忽略",
        f"default = {_s(cfg.mode.default)}",
        f"fallback = {_s(cfg.mode.fallback)}",
        "",
        "[jailbreak]",
        f"enabled = {'true' if cfg.jailbreak.enabled else 'false'}",
        f"start_level = {cfg.jailbreak.start_level}",
        f"max_level = {cfg.jailbreak.max_level}",
        f"ladder_timeout_ms = {cfg.jailbreak.ladder_timeout_ms}",
    ]

    for p in cfg.providers:
        lines += [
            "",
            "[[providers]]",
            f"name = {_s(p.name)}",
            "# kind: openai（含所有 OpenAI 兼容接口）/ anthropic / gemini / custom",
            f"kind = {_s(p.kind)}",
            f"base_url = {_s(p.base_url)}",
            f"enabled = {'true' if p.enabled else 'false'}",
            "# 这家能服务的模型清单（候选池按 账号×模型 展开）",
            f"models = {_arr(list(p.models))}",
        ]
        for acc in p.accounts:
            # key_env 是环境变量名 —— 密钥本身绝不写进这个文件
            lines += [
                "",
                "[[providers.accounts]]",
                f"key_id = {_s(acc.key_id)}",
                f"key_env = {_s(acc.key_env)}",
                f"quota = {_s(acc.quota)}",
            ]

    lines.append("")
    return "\n".join(lines)


def save_secrets(path, secrets: dict[str, str]) -> None:
    """把本次提交的密钥**合并**写进 secrets.toml。

    合并语义：文件里已有、本次没提交的键原样保留 —— 设置页改一家
    供应商的 key，不能把别家的弄丢。同名键覆盖（换 key 了）。
    空提交直接返回（不动文件）。

    密钥文件为什么不用环境变量：env 写入是平台相关的（Windows setx /
    Linux export），而且只对新进程生效；一个本地文件 Windows/Linux
    通吃，网关在哪跑都一样。
    """
    if not secrets:
        return
    from pathlib import Path

    import tomllib

    p = Path(path)
    existing: dict[str, str] = {}
    if p.is_file():
        try:
            with p.open("rb") as fh:
                data = tomllib.load(fh)
            existing = {k: v for k, v in data.items() if isinstance(v, str)}
        except Exception as exc:                   # noqa: BLE001
            _warn_local(f"密钥文件 {p} 读不了（{exc}），将整体重写 —— "
                        f"建议先备份")
            existing = {}

    merged = {**existing, **secrets}
    lines = ["# 本机密钥文件 —— 不进 git，不要分享这个文件",
             ""]
    lines += [f"{k} = {_s(v)}" for k, v in merged.items()]
    lines.append("")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, p)


def _warn_local(msg: str) -> None:
    import sys
    print(f"[gateway] {msg}", file=sys.stderr)
