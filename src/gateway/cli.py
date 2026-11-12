"""G9 CLI —— 命令行入口。

## 为什么用纯 print 而不是 rich

KXNS-NG 那次踩过：`console.print(text)` 没关 markup，靶场返回里带 `[/xxx]`，
rich 当 markup 解析然后抛 MarkupError，**整份报告丢失**，而且表现为「任务失败」——
打穿了记成没打穿。

这里的结论不是「记得加 markup=False」，而是：**自用工具的表格不值得引入 rich**。
纯 print + 手工对齐就够，少一个能把输出炸掉的依赖。

## 编码兜底

CI 里 locale 常是 C/POSIX（`PYTHONIOENCODING=ascii`），直接 print 中文会
`UnicodeEncodeError`。`_out()` 统一做兜底：编不出来就转义，不崩。

## 退出码契约

    --help            0
    不带参数           0（给帮助，不是报错）
    未知子命令         2（argparse 默认）
    子命令 --help      0
"""

from __future__ import annotations

import argparse
import sys

from .jailbreak.refusal import detect
from .jailbreak.technique import (
    Level,
    get_technique,
    list_techniques,
    select_techniques,
)
from .mode.manual import FallbackPolicy, parse_target
from .providers.aggregator import Aggregator
from .providers.anthropic import AnthropicAdaptor
from .providers.deepseek import DeepSeekAdaptor
from .providers.gemini import GeminiAdaptor
from .providers.openai import OpenAIAdaptor
from .routing.pool import Candidate, Pool, RoutingPolicy


# ═══════════════════════════════════════════════════════════════
# 输出（编码安全）
# ═══════════════════════════════════════════════════════════════
def _out(text: str = "") -> None:
    """编码安全的打印。

    C/POSIX locale 下 stdout 是 ascii，直接 print 中文会 UnicodeEncodeError。
    转义而不是崩 —— 用户宁可看到 \\u4e2d 也不想看 traceback。
    """
    try:
        print(text)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", "ascii") or "ascii"
        print(text.encode(enc, "backslashreplace").decode(enc, "replace"))


def _table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> None:
    """手工对齐的表格。不用 rich —— 见模块 docstring。"""
    all_rows = [headers] + rows
    widths = [max(len(str(r[i])) for r in all_rows) for i in range(len(headers))]
    for r in all_rows:
        line = "  ".join(str(c).ljust(w) for c, w in zip(r, widths))
        _out(line.rstrip())
    _out("  ".join("─" * w for w in widths))


# ═══════════════════════════════════════════════════════════════
# providers
# ═══════════════════════════════════════════════════════════════
def _build_aggregator() -> Aggregator:
    agg = Aggregator()
    agg.register("openai", OpenAIAdaptor())
    agg.register("anthropic", AnthropicAdaptor())
    agg.register("deepseek", DeepSeekAdaptor())
    agg.register("gemini", GeminiAdaptor())
    return agg


_PROTOCOL_NOTE = {
    "openai": "基准格式（messages/temperature/max_tokens）",
    "anthropic": "system 顶层独立 / max_tokens 必填 / content 是 block 数组",
    "deepseek": "OpenAI 完全兼容，只换 base_url",
    "gemini": "contents+parts / systemInstruction / generationConfig / ?key= 鉴权",
}


def cmd_providers_list(_args) -> int:
    agg = _build_aggregator()
    _out("已注册的 provider：")
    _out()
    rows = []
    for name in agg.names():
        a = agg.get(name)
        rows.append((name, a.name, _PROTOCOL_NOTE.get(name, "")))
    _table(rows, ("注册名", "适配器", "协议差异"))
    _out()
    _out(f"共 {len(agg.names())} 家")
    return 0


# ═══════════════════════════════════════════════════════════════
# jailbreak
# ═══════════════════════════════════════════════════════════════
def cmd_jailbreak_list(args) -> int:
    lang = args.lang
    techs = list_techniques(level=Level(args.level) if args.level else None,
                            lang=lang)
    _out(f"破甲技术（{len(techs)} 种）")
    if lang:
        _out(f"语言过滤: {lang}")
    _out()
    rows = []
    for t in techs:
        rows.append((t.name, f"L{int(t.level)}", t.cost,
                     ",".join(t.langs), t.description[:38]))
    _table(rows, ("名称", "层级", "成本", "语言", "说明"))
    return 0


def cmd_jailbreak_transform(args) -> int:
    try:
        t = get_technique(args.tech)
    except KeyError as e:
        _out(f"错误：{e}")
        return 2
    _out(f"技术: {t.name}  (L{int(t.level)}, {t.cost})")
    _out(f"说明: {t.description}")
    _out()
    _out("原始:")
    _out(f"  {args.query}")
    _out()
    _out("变形后:")
    try:
        out = t.transform(args.query)
    except Exception as e:                       # noqa: BLE001
        _out(f"  变形失败: {type(e).__name__}: {e}")
        return 1
    for line in str(out).splitlines():
        _out(f"  {line}")
    return 0


def cmd_jailbreak_refusal(args) -> int:
    text = " ".join(args.text) if args.text else ""
    v = detect(text)
    if v.is_refusal:
        _out(f"判定: 拒绝（{v.layer} 层）")
    elif v.partial:
        _out(f"判定: 先拒后答（{v.layer} 层）—— 拿到部分内容")
    else:
        _out("判定: 非拒绝（正常回答）")
    _out(f"理由: {v.reason or '无'}")
    _out(f"长度: {len(text)} 字符")
    return 0


# ═══════════════════════════════════════════════════════════════
# route
# ═══════════════════════════════════════════════════════════════
def cmd_route(args) -> int:
    """解释一次选路：候选有哪些、谁入选、谁被排除、为什么。"""
    pool = Pool()

    # 演示用的候选池（真实使用时从配置读）
    cands = [
        Candidate(provider="openai", account="key1", model="gpt-4o"),
        Candidate(provider="openai", account="key2", model="gpt-4o"),
        Candidate(provider="deepseek", account="key1", model="deepseek-chat"),
        Candidate(provider="anthropic", account="key1", model="claude-3-5-sonnet"),
    ]

    # 人为造一个故障，让解释有东西可说
    dead = cands[2]
    for _ in range(3):
        pool.record_failure(dead, Exception("演示用故障"),
                            lease=pool.acquire(dead))

    _out(f"请求型号: {args.model}")
    if args.target:
        try:
            t = parse_target(args.target)
            _out(f"手动目标: {t}  (锁定粒度: {t.lock})")
        except ValueError as e:
            _out(f"目标解析失败: {e}")
            return 2
        _out(f"降级策略: {args.policy}")
    else:
        _out("模式: auto（型号亲和）")
    _out()

    policy = RoutingPolicy()
    rows = []
    for c in cands:
        if pool.is_cooled(c.provider):
            rows.append((str(c), "排除", "provider 冷却中", "-"))
        elif pool.is_account_backoff(c.provider, c.account):
            rows.append((str(c), "排除", "账号配额耗尽", "-"))
        else:
            same = "同型号" if c.model == args.model else "异型号"
            rows.append((str(c), "入选", same, f"{pool.score(c, policy):.3f}"))

    _table(rows, ("候选", "结果", "原因", "得分"))
    _out()

    picked = pool.rank_targets(cands, policy)
    picked = [c for c in picked if c.model == args.model] or picked
    if picked:
        _out(f"最终会选: {picked[0]}")
    else:
        _out("最终会选: 无（全部不可用）")
    return 0


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gateway",
        description="AI 路由网关 —— 多 provider 聚合、自动降级、破甲",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  gateway providers list\n"
            "  gateway jailbreak list --lang zh\n"
            "  gateway jailbreak transform '如何提权' --tech base64\n"
            "  gateway jailbreak refusal \"I'm sorry, but I can't help\"\n"
            "  gateway route --model gpt-4o\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd", metavar="<命令>")

    sp = sub.add_parser("providers", help="provider 相关")
    sp_sub = sp.add_subparsers(dest="sub", metavar="<子命令>")
    sp_list = sp_sub.add_parser("list", help="列出已注册的 provider")
    sp_list.set_defaults(func=cmd_providers_list)

    jb = sub.add_parser("jailbreak", help="破甲相关")
    jb_sub = jb.add_subparsers(dest="sub", metavar="<子命令>")

    jb_list = jb_sub.add_parser("list", help="列出破甲技术")
    jb_list.add_argument("--lang", choices=["en", "zh"], default=None,
                         help="按语言过滤")
    jb_list.add_argument("--level", type=int, choices=[0, 1, 2, 3, 4], default=None,
                         help="按阶梯层过滤")
    jb_list.set_defaults(func=cmd_jailbreak_list)

    jb_tr = jb_sub.add_parser("transform", help="对一段文本做变形")
    jb_tr.add_argument("query", help="原始文本")
    jb_tr.add_argument("--tech", required=True, help="技术名（见 jailbreak list）")
    jb_tr.set_defaults(func=cmd_jailbreak_transform)

    jb_rf = jb_sub.add_parser("refusal", help="判定一段回答是不是拒绝")
    jb_rf.add_argument("text", nargs="*", help="待判定文本")
    jb_rf.set_defaults(func=cmd_jailbreak_refusal)

    rt = sub.add_parser("route", help="解释一次选路")
    rt.add_argument("--model", required=True, help="请求的型号")
    rt.add_argument("--target", default=None, help="手动目标（provider:账号/型号）")
    rt.add_argument("--policy", default=FallbackPolicy.STRICT.value,
                    choices=[p.value for p in FallbackPolicy], help="降级策略")
    rt.set_defaults(func=cmd_route)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "cmd", None):
        # 不带参数 → 给帮助，退出码 0。用户敲 `gateway` 是想知道能干什么。
        parser.print_help()
        return 0

    if not hasattr(args, "func"):
        # 有子命令但没给子命令名 → 给该子命令的帮助
        parser.parse_args([args.cmd, "--help"])
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
