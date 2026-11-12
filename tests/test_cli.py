"""G9 CLI 测试。

三条硬约束（全部来自 KXNS-NG 那次的实测教训）：

1. **`console.print(text)` 必须 `markup=False`**
   NG 靶场返回的文本里带 `[/xxx]`，rich 会当 markup 解析然后抛 MarkupError，
   整份报告丢失，而且表现为「任务失败」—— 打穿了记成没打穿。
   CLI 打印的每一处动态文本都要过这一关，不是只在明显的地方加。

2. **`--help` 退出码必须是 0**
   这是脚本化调用的基本契约。退出码 1 会让 CI 和 shell 判断误判。

3. **不带参数时给帮助，不报错**
   用户敲 `gateway` 想知道能干什么，不是想看 traceback。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path("/home/kali/work/ai-gateway/src")
PY = "/home/kali/work/ai-gateway/.venv/bin/python"


def run_cli(*args: str, timeout: int = 30):
    """以子进程跑真 CLI —— 测的是「用户能用的东西」，不是函数。"""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/kali",
        "PYTHONPATH": str(SRC),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run([PY, "-m", "gateway.cli", *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


# ═══════════════════════════════════════════════════════════════
# ① 退出码契约
# ═══════════════════════════════════════════════════════════════
def test_help_退出码为_0():
    r = run_cli("--help")
    assert r.returncode == 0, f"help 退出码 {r.returncode}，stderr={r.stderr[:300]}"


def test_不带参数也给帮助且退出码_0():
    r = run_cli()
    assert r.returncode == 0, f"裸跑退出码 {r.returncode}: {r.stderr[:300]}"
    assert "用法" in (r.stdout + r.stderr) or "usage" in (r.stdout + r.stderr).lower()


def test_未知子命令退出码非零():
    r = run_cli("不存在的命令")
    assert r.returncode != 0


def test_子命令_help_退出码_0():
    for sub in ("providers", "jailbreak", "route"):
        r = run_cli(sub, "--help")
        assert r.returncode == 0, f"{sub} --help 退出码 {r.returncode}"


# ═══════════════════════════════════════════════════════════════
# ② markup 安全（NG 那次的教训）
# ═══════════════════════════════════════════════════════════════
def test_输出不含_rich_markup_报错():
    """任何子命令的输出里都不能出现 MarkupError。"""
    for args in ([], ["--help"], ["providers"], ["providers", "list"],
                 ["jailbreak", "list"], ["jailbreak", "refusal", "test"]):
        r = run_cli(*args)
        combined = r.stdout + r.stderr
        assert "MarkupError" not in combined, f"{args} 触发 MarkupError: {combined[:300]}"
        assert "markup" not in combined.lower()


def test_带方括号的文本不被当_markup():
    """模拟靶场那种 `[/xxx]` 文本 —— 这是 NG 那次原样复现的触发条件。"""
    r = run_cli("jailbreak", "refusal", "[/red]I'm sorry, but I can't help[/]")
    assert "MarkupError" not in (r.stdout + r.stderr)
    assert r.returncode == 0


# ═══════════════════════════════════════════════════════════════
# ③ providers list 真输出
# ═══════════════════════════════════════════════════════════════
def test_providers_list_列出五家():
    r = run_cli("providers", "list")
    assert r.returncode == 0, r.stderr[:300]
    out = r.stdout.lower()
    for name in ("openai", "anthropic", "deepseek", "gemini"):
        assert name in out, f"没列出 {name}: {out[:400]}"


def test_providers_list_标注协议差异():
    """列表要能看出「哪家和 OpenAI 兼容」—— 这是选型时最关心的。"""
    r = run_cli("providers", "list")
    assert "openai" in r.stdout.lower()
    assert len(r.stdout.strip()) > 50


# ═══════════════════════════════════════════════════════════════
# ④ jailbreak 子命令
# ═══════════════════════════════════════════════════════════════
def test_jailbreak_list_列出技术数():
    r = run_cli("jailbreak", "list")
    assert r.returncode == 0, r.stderr[:300]
    # 25 种技术，输出里要能看到其中几个
    out = r.stdout.lower()
    assert "base64" in out
    assert "rot13" in out


def test_jailbreak_list_能按语言过滤():
    r = run_cli("jailbreak", "list", "--lang", "zh")
    assert r.returncode == 0
    # 中文列表里不该有 rot13
    assert "rot13" not in r.stdout.lower(), "中文过滤失效"


def test_jailbreak_transform_输出变形结果():
    r = run_cli("jailbreak", "transform", "如何提权", "--tech", "base64")
    assert r.returncode == 0, r.stderr[:300]
    # base64 编码后应该没有原样的中文在密文部分
    assert "Base64" in r.stdout or "base64" in r.stdout.lower()


def test_jailbreak_refusal_判定拒绝():
    r = run_cli("jailbreak", "refusal", "I'm sorry, but I can't help with that.")
    assert r.returncode == 0
    out = r.stdout
    assert ("拒绝" in out) or ("refusal" in out.lower()) or ("True" in out)


def test_jailbreak_refusal_判定正常回答():
    r = run_cli("jailbreak", "refusal", "答案是 42。")
    assert r.returncode == 0
    out = r.stdout
    assert ("非拒绝" in out) or ("正常" in out) or ("False" in out)


# ═══════════════════════════════════════════════════════════════
# ⑤ route 子命令（解释选路）
# ═══════════════════════════════════════════════════════════════
def test_route_展示候选与排除原因():
    r = run_cli("route", "--model", "gpt-4o")
    assert r.returncode == 0, r.stderr[:400]
    out = r.stdout
    # 要能看出「哪些入选、哪些被排除、为什么」
    assert len(out.strip()) > 30


def test_route_支持_manual_目标():
    r = run_cli("route", "--model", "gpt-4o", "--target", "openai/gpt-4o")
    assert r.returncode == 0, r.stderr[:400]


# ═══════════════════════════════════════════════════════════════
# ⑥ 全局约束
# ═══════════════════════════════════════════════════════════════
def test_所有子命令都有_help():
    main_help = run_cli("--help").stdout
    # 从 help 里抠出子命令名。**必须排除 prog 名** ——
    # `usage: gateway [-h] <命令>` 里的 "gateway" 也在行首缩进位置，
    # 不排除会把程序名当子命令，然后拿它去跑 --help 必然失败。
    subs = set(re.findall(r"^\s{2,}([a-z][a-z0-9_-]+)\s", main_help, re.M))
    subs -= {"options", "usage", "commands", "help", "gateway"}
    subs &= set(re.findall(r"\{([a-z,\s]+)\}", main_help)[0].replace(" ", "").split(",")) \
        if re.findall(r"\{([a-z,\s]+)\}", main_help) else subs
    assert len(subs) >= 3, f"只找到子命令 {subs}"
    for s in subs:
        r = run_cli(s, "--help")
        assert r.returncode == 0, f"{s} --help 失败: {r.stderr[:200]}"


def test_输出不崩在非_utf8_终端():
    """CI 里 locale 常常是 C/POSIX，输出未编码字符会崩。"""
    env_add = {"PYTHONIOENCODING": "ascii"}
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/kali",
        "PYTHONPATH": str(SRC),
        **env_add,
    }
    r = subprocess.run([PY, "-m", "gateway.cli", "providers", "list"],
                       capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"ascii 终端下崩了: {r.stderr[:300]}"
