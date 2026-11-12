"""设置页的纯逻辑层 —— GatewayConfig ⇄ TOML 文本 + 密钥写环境变量。

## 为什么手写序列化而不用 toml 库

Python 标准库只有读（tomllib），没有写；引第三方 toml 写库只为了
一个固定结构的文件不值当（项目的「少一个依赖少一个炸点」哲学，
参考 cli.py 不用 rich 的决策）。结构是我们自己定的、字段有限，
手写生成最透明 —— 出问题一眼能看出是哪行。

字符串转义用 json.dumps：它产出的转义集（\\" \\\\ \\uXXXX）
是 TOML 基本字符串（basic string）的合法子集，中文原样保留。

## 密钥的安全边界

密钥值**永远不进 config.toml**（文件可能被分享/进仓库/被同步盘泄露）。
对话框里填的密钥走 set_user_env_var：
  当前进程：os.environ 立即生效（保存后热重载直接能用）
  以后的进程：Windows 用 setx 写用户环境变量（开机自启场景）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from ..config import GatewayConfig

__all__ = ["serialize_config", "set_user_env_var", "auto_key_env"]


def auto_key_env(provider_name: str, index: int) -> str:
    """按供应商名自动生成密钥环境变量名：deepseek → DEEPSEEK_KEY_1。

    用户不该手动填变量名 —— 这是实现细节。规则：非 ASCII 字母数字
    一律转下划线（中文名会退化成空前缀，但仍是合法变量名），
    避免 setx 拒收或读取时对不上。
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


def set_user_env_var(key: str, value: str) -> None:
    """密钥 → 用户环境变量。当前进程立即生效；Windows 加 setx 持久化。

    setx 只对新开的进程生效（本进程靠 os.environ 那行），这是 Windows
    的机制，不是我们的限制。写入失败（比如策略限制）抛异常给调用方提示。
    """
    os.environ[key] = value
    if sys.platform == "win32":
        subprocess.run(["setx", key, value], check=True, capture_output=True)
